#!/usr/bin/env python
import rospy
from anti_instagram.AntiInstagram_rebuild import *
from anti_instagram.kmeans_rebuild import *
from cv_bridge import CvBridge  # @UnresolvedImport
from duckietown_msgs.msg import (AntiInstagramHealth, AntiInstagramTransform, AntiInstagramTransform_CB, BoolStamped)
from duckietown_utils.jpg import image_cv_from_jpg
from line_detector.timekeeper import TimeKeeper
from sensor_msgs.msg import CompressedImage, Image  # @UnresolvedImport
import numpy as np
import rospy
import os
from anti_instagram.geom import processGeom2

"""
This node subscribed to the uncorrected images from the camera. Within a certain time interval (defined from
    commandline) this node calculates and publishes the image transform. It can be calculated either by using only
    a basic color balance method, or with a computationally more expensive linear transformation. Further its possible 
    to use both methods.
    
    This node publishes the parameters for the linear transform as well as for the color balance based transform. 
    Other than that, the node publishes the mask that was used. 
"""


class ContAntiInstagramNode():
    def __init__(self):
        self.node_name = rospy.get_name()
        robot_name = rospy.get_param("~veh", "") #to read the name always reliably

        self.active = True
        self.locked = False

        # Initialize publishers and subscribers
        self.pub_trafo = rospy.Publisher(
            "~transform", AntiInstagramTransform, queue_size=1)
        self.pub_trafo_CB = rospy.Publisher(
            "~colorBalanceTrafo", AntiInstagramTransform_CB, queue_size=1)
        self.pub_health = rospy.Publisher(
            "~health", AntiInstagramHealth, queue_size=1, latch=True)
        self.pub_mask = rospy.Publisher(
            "~mask", Image, queue_size=1)
        self.pub_geomImage = rospy.Publisher(
            "~geomImage", Image, queue_size=1)
        self.sub_image = rospy.Subscriber(
            '/{}/camera_node/image/compressed'.format(robot_name), CompressedImage, self.cbNewImage, queue_size=1)


        # Verbose option
        self.verbose = rospy.get_param('line_detector_node/verbose', True)

        # Read parameters
        self.interval = self.setupParameter("~ai_interval", 10)
        self.fancyGeom = self.setupParameter("~fancyGeom", False)
        self.n_centers = self.setupParameter("~n_centers", 10)
        self.blur = self.setupParameter("~blur", 'median')
        self.resize = self.setupParameter("~resize", 0.2)
        self.blur_kernel = self.setupParameter("~blur_kernel", 5)
        self.cb_percentage = self.setupParameter("~cb_percentage", 2)
        self.trafo_mode = self.setupParameter("~trafo_mode", 'cb')
        if not (self.trafo_mode == "cb" or self.trafo_mode == "lin" or self.trafo_mode == "both"):
            rospy.loginfo("cannot understand argument 'trafo_mode'. set to 'both' ")
            self.trafo_mode == "both"
            rospy.set_param("~trafo_mode", "both")  # Write to parameter server for transparancy
            rospy.loginfo("[%s] %s = %s " % (self.node_name, "~trafo_mode", "both"))


        # Initialize health message
        self.health = AntiInstagramHealth()

        # Initialize transform message
        self.transform = AntiInstagramTransform()
        # FIXME: read default from configuration and publish it

        # initialize color balance transform message
        self.transform_CB = AntiInstagramTransform_CB()

        # initialize AI class
        self.ai = AntiInstagram()
        self.ai.setupKM(self.n_centers, self.blur, 1, self.blur_kernel)

        # initialize msg bridge
        self.bridge = CvBridge()

        self.image_msg = None

        # timer for continuous image process
        self.timer_init = rospy.Timer(rospy.Duration(self.interval), self.processImage)
        self.timer_cont = None
        self.timer_counter = 0
        rospy.loginfo('ai: Looking for initial trafo.')

        # bool to switch from initialisation to continuous mode
        self.initialized = False

        # container for mask and maskedImage
        self.mask255 = []
        self.geomImage = []

    def setupParameter(self, param_name, default_value):
        value = rospy.get_param(param_name, default_value)
        rospy.set_param(param_name, value)#Write to parameter server for transparancy
        rospy.loginfo("[%s] %s = %s " % (self.node_name, param_name, value))
        return value

    def cbNewImage(self, image_msg):
        # memorize image
        self.image_msg = image_msg

    def processImage(self, event):
        # processes image with either color balance, linear trafo or both

        # if we have seen an image:
        if self.image_msg is not None:
            tk = TimeKeeper(self.image_msg)

            try:
                cv_image = image_cv_from_jpg(self.image_msg.data)
            except ValueError as e:
                rospy.loginfo('Anti_instagram cannot decode image: %s' % e)
                return

            tk.completed('converted')

            # resize input image
            resized_img = cv2.resize(cv_image, (0, 0), fx=self.resize, fy=self.resize)
            tk.completed('resized')

            H, W, D = resized_img.shape

            # apply geometry
            if self.fancyGeom:
                # apply fancy geom
                mask = self.mask = processGeom2(resized_img)
                self.mask255 = mask
                self.mask255[self.mask255 == 1] = 255
                self.geomImage = np.expand_dims(mask, axis=-1) * resized_img

                tk.completed('fancyGeom')
                # self.geomImage = np.transpose(np.stack((mask, mask, mask), axis=2)) * resized_img
                # idx = (mask==1)
                # self.geomImage = np.zeros((H, W), np.uint8)
                # self.geomImage = resized_img[idx]
            else:
                # remove upper part of the image
                self.geomImage = resized_img[int(H * 0.3):(H - 1), :, :]
                tk.completed('notFancyGeom')

            # apply color balance if required
            if self.trafo_mode == "cb" or self.trafo_mode == "both":
                # find color balance thresholds
                self.ai.calculateColorBalanceThreshold(self.geomImage, self.cb_percentage)
                tk.completed('calculateColorBalanceThresholds')

                # store color balance thresholds to ros message
                self.transform_CB.th[0], self.transform_CB.th[1], self.transform_CB.th[2] = self.ai.ThLow
                self.transform_CB.th[3], self.transform_CB.th[4], self.transform_CB.th[5] = self.ai.ThHi

                # publish color balance thresholds
                self.pub_trafo_CB.publish(self.transform_CB)
                rospy.loginfo('ai: Color balance thresholds published.')

                tk.completed('colorBalance analysis')


            # apply linear trafo if required
            if self.trafo_mode == "lin" or self.trafo_mode == "both":
                # take in account the previous color balance
                if self.trafo_mode == "both":
                    # apply color balance
                    colorBalanced_image = self.ai.applyColorBalance(self.geomImage, self.ai.ThLow, self.ai.ThHi)
                else:
                    # pass image without color balance trafo
                    colorBalanced_image = self.geomImage

                tk.completed('passed image to linear trafo')

                # not yet initialized
                if not self.initialized:
                    # apply bounded trafo
                    if self.ai.calculateBoundedTransform(colorBalanced_image):
                        # init successful. set interval on desired by input
                        self.initialized = True
                        self.timer_init.shutdown()
                        self.timer_cont = rospy.Timer(rospy.Duration(self.interval), self.processImage)

                        # store color transform to ros message
                        self.transform.s[0], self.transform.s[1], self.transform.s[2] = self.ai.shift
                        self.transform.s[3], self.transform.s[4], self.transform.s[5] = self.ai.scale

                        # publish color trafo
                        self.pub_trafo.publish(self.transform)
                        rospy.loginfo('ai: Initial trafo found and published! Switch to continuous mode.')
                    else:
                        rospy.loginfo('ai: average error too large. transform NOT updated.')


                # initialisation already done: continuous mode
                else:
                    if self.timer_counter == 0:
                        # perform linear transform only every n seconds

                        # find color transform
                        # if self.ai.calculateTransform(colorBalanced_image):
                        if self.ai.calculateBoundedTransform(colorBalanced_image):
                            tk.completed('calculateTransform')

                            # store color transform to ros message
                            self.transform.s[0], self.transform.s[1], self.transform.s[2] = self.ai.shift
                            self.transform.s[3], self.transform.s[4], self.transform.s[5] = self.ai.scale

                            # publish color trafo
                            self.pub_trafo.publish(self.transform)
                        else:
                            rospy.loginfo('ai: average error too large. transform NOT updated.')

                        # increase counter
                        self.timer_counter = self.timer_counter + 1
                    else:
                        # do not perform linear trafo here
                        # increase counter
                        self.timer_counter = self.timer_counter + 1
                        if self.timer_counter == self.interval:
                            self.timer_counter = 0

            if self.fancyGeom:
                self.mask = self.bridge.cv2_to_imgmsg(
                    self.mask255, "mono8")
                self.pub_mask.publish(self.mask)
                rospy.loginfo('published mask!')

            geomImgMsg = self.bridge.cv2_to_imgmsg(
                self.geomImage, "bgr8")
            self.pub_geomImage.publish(geomImgMsg)

            if self.verbose:
                rospy.loginfo('ai:\n' + tk.getall())



if __name__ == '__main__':
    # Initialize the node with rospy
    rospy.init_node('cont_anti_instagram_node', anonymous=False)

    # Create the NodeName object
    node = ContAntiInstagramNode()

    # Setup proper shutdown behavior
    #rospy.on_shutdown(node.on_shutdown)
    # Keep it spinning to keep the node alive
    rospy.spin()
