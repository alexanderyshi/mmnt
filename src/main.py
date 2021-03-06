# sudo chmod 666 /dev/ttyACM0
# lsusb - find which usb device corresponds to the mic
# sudo chmod o+rw /dev/bus/usb/001/003

import time
import sys
import usb.core
import usb.util
import serial
import logging
import subprocess
from threading import Thread

import cv2 as cv
import numpy as np
import human_tracking as ht

from tf_pose.estimator import TfPoseEstimator
from tf_pose.networks import get_graph_path, model_wh
from tf_pose.common import CocoPart

from tuning import Tuning
from motor_control import MotorControl
from video_stream import VideoStream
from mapping.mapping import Map

import os, fcntl
import v4l2

DEBUG = True
DISPLAY_VIDEO = False
DRAW_ON_FRAME = False

RESIZE_RATIO = 4.0
TF_MODEL = "mobilenet_thin" # alternative option: "cmu"
ANGLE_THRESHOLD = 3
NOISE_ANGLE_THRESHOLD = 5
FACE_THRESHOLD = float(VideoStream.DEFAULT_WIDTH) / 3.0 / 2.0 + 40
FOV = 60
degreePerPixel = float(FOV) / float(VideoStream.DEFAULT_WIDTH)

HUMAN_SAMPLE_FREQ = 1.75
MIC_SAMPLE_FREQ   = 0.1
OVERLAP_THRESHOLD = 45

headParts = [CocoPart.Nose.value,
             CocoPart.REye.value,
             CocoPart.LEye.value,
             CocoPart.REar.value,
             CocoPart.LEar.value,
             CocoPart.Neck.value]

class State:
    IDLE  = 0x00
    NOISE = 0x01
    HUMAN = 0x02
    BOTH  = 0x03

class Cams:
    BOT = "0"
    TOP = "1"


class Moment(object):
    def __init__(self):
        self.stop = False
        logging.basicConfig()
        self.logger = logging.getLogger("MMNT")
        if DEBUG:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)
        self.logger.info("Initializing")

        self.masterSampleTime = time.time()
        self.slaveSampleTime = time.time()

        self.humanSampleTime = time.time()
        self.micSampleTime = time.time()

        self.logger.debug("Initializing motor control")
        self.mc = MotorControl()
        self.mc.resetMotors()

        self.logger.debug("Initializing microphone")
        dev = usb.core.find(idVendor=0x2886, idProduct=0x0018)
        if not dev:
            sys.exit("Could not find ReSpeaker Mic Array through USB")
        self.mic = Tuning(dev)
        self.mic.write("NONSTATNOISEONOFF", 1)
        self.mic.write("STATNOISEONOFF", 1)
        self.mic.write("ECHOONOFF", 1)


        self.logger.debug("Initializing video streams")
        self.topCamStream = VideoStream(1)
        self.botCamStream = VideoStream(2)
        
        self.logger.debug("Initializing models")
        self.ht_model = ht.get_model()
        self.tfPose = TfPoseEstimator(get_graph_path(TF_MODEL), target_size=(VideoStream.DEFAULT_WIDTH, VideoStream.DEFAULT_HEIGHT))
        self.logger.info("Initialization complete")

        self.topCamState = State.IDLE
        self.botCamState = State.IDLE

        self.topCamAngle = 0
        self.topAngleUpdated = False
        self.botCamAngle = 180
        self.botAngleUpdated = False
        self.master = Cams.TOP
        self.lastMaster = Cams.TOP

        self.botCamProc = None
        self.topCamProc = None

        self.audioMap = Map(15)
        self.checkMic()

    def stop(self):
        self.stop = True

    def updateTopAngle(self, angle):
        if abs(angle - self.topCamAngle) > ANGLE_THRESHOLD and abs(angle - self.botCamAngle) > OVERLAP_THRESHOLD:
            self.topCamAngle = angle
            self.topAngleUpdated = True

    def updateBotAngle(self, angle):
        if abs(angle - self.botCamAngle) > ANGLE_THRESHOLD and abs(angle - self.topCamAngle) > OVERLAP_THRESHOLD:
            self.botCamAngle = angle
            self.botAngleUpdated = True

    def updatePositions(self):
        # Send Serial Commands
        if self.topAngleUpdated and self.botAngleUpdated:
            self.logger.debug("Top Angle: {}".format(self.topCamAngle))
            self.logger.debug("Bot Angle: {}".format(self.botCamAngle))
            self.topAngleUpdated = False
            self.botAngleUpdated = False
            self.mc.runMotors(self.topCamAngle, self.botCamAngle)
        elif self.topAngleUpdated:
            self.logger.debug("Top Angle: {}".format(self.topCamAngle))
            self.topAngleUpdated = False
            self.mc.runTopMotor(self.topCamAngle)
        elif self.botAngleUpdated:
            self.logger.debug("Bot Angle: {}".format(self.botCamAngle))
            self.botAngleUpdated = False
            self.mc.runBotMotor(self.botCamAngle)

    def isWithinNoiseFov(self, angle):
        topDiff = abs(angle - self.topCamAngle)
        botDiff = abs(angle - self.botCamAngle)

        if topDiff < NOISE_ANGLE_THRESHOLD:
            self.topCamState |= State.NOISE
            if self.topCamState == State.BOTH:
                self.master = Cams.TOP
            return True
        else:
            self.topCamState &= ~State.NOISE
        if botDiff < NOISE_ANGLE_THRESHOLD:
            self.botCamState |= State.NOISE
            if self.botCamState == State.BOTH:
                self.master = Cams.BOT
            return True
        else:
            self.botCamState &= ~State.NOISE

        return False

    def checkMic(self):
        speechDetected, micDOA = self.mic.speech_detected(), self.mic.direction
        if not speechDetected:
            # self.audioMap.update_map_with_no_noise()
            self.topCamState &= ~State.NOISE
            self.botCamState &= ~State.NOISE
            return
        self.logger.debug("speech detected from {}".format(micDOA))
        self.audioMap.update_map_with_noise(micDOA)

        primaryMicDOA, secondaryMicDOA = self.audioMap.get_POI_location()
        if DEBUG:
            self.audioMap.print_map()
        if primaryMicDOA == -1:
            self.logger.debug("no good audio source")
            return

        self.logger.debug("mapped audio from {}".format(primaryMicDOA))

        # Check if camera is already looking at the primary noise source
        if self.isWithinNoiseFov(primaryMicDOA):
            # If camera is already looking, check the secondary noise source
            if secondaryMicDOA == -1:
                self.logger.debug("no good secondary audio source")
                return
            elif self.isWithinNoiseFov(secondaryMicDOA):
                return
            else:
                micDOA = secondaryMicDOA
        else:
            micDOA = primaryMicDOA

        topDiff = abs(micDOA - self.topCamAngle)
        botDiff = abs(micDOA - self.botCamAngle)

        # Camera is NOT looking at the noise source at this point
        # If both Cameras are not tracking a human,
        # move the closest camera
        if self.topCamState < State.HUMAN and self.botCamState < State.HUMAN:
            if botDiff < topDiff:
                self.botCamState |= State.NOISE
                self.updateBotAngle(micDOA)
                if self.botCamState == State.IDLE:
                    self.master = Cams.TOP
            else:
                self.topCamState |= State.NOISE
                self.updateTopAngle(micDOA)
                if self.topCamState == State.IDLE:
                    self.master = Cams.BOT
        # One of the cameras are on a human, if the other camera is not on a human, move it
        elif self.topCamState < State.HUMAN:
            self.topCamState |= State.NOISE
            self.updateTopAngle(micDOA)
            self.master = Cams.BOT
        elif self.botCamState < State.HUMAN:
            self.botCamState |= State.NOISE
            self.updateBotAngle(micDOA)
            self.master = Cams.TOP
        # The cameras are on a human
        else:
            # If both are on a human, move the one that's not master
            if self.topCamState == State.HUMAN and self.botCamState == State.HUMAN:
                if self.master != Cams.BOT:
                    self.botCamState |= State.NOISE
                    self.updateBotAngle(micDOA)
                else:
                    self.topCamState |= State.NOISE
                    self.updateTopAngle(micDOA)
            # One of the cameras are on a HUMAN+NOISE, move the one that's not only on a HUMAN
            elif self.topCamState == State.HUMAN:
                self.topCamState |= State.NOISE
                self.updateTopAngle(micDOA)
                self.master = Cams.BOT
            elif self.botCamState == State.HUMAN:
                self.botCamState |= State.NOISE
                self.updateBotAngle(micDOA)
                self.master = Cams.TOP

    def getBestFace(self, humans):
        midX = -1
        bestHuman = humans[0]
        maxScore = 0
        for human in humans:
            gotMidX = False
            score = 0
            currMidX = -1
            for part in headParts:
                if part in human.body_parts:
                    score += human.body_parts[part].score
                    if not gotMidX:
                        currMidX = human.body_parts[part].x * VideoStream.DEFAULT_WIDTH
                        gotMidX = True
            if score > maxScore:
                maxScore = score
                midX = currMidX
                bestHuman = human

        return bestHuman, midX

    def checkHumans(self, frame, camera):
        humans = self.tfPose.inference(frame, resize_to_default=True, upsample_size=RESIZE_RATIO)
        if len(humans):
            if camera == Cams.TOP:
                self.topCamState |= State.HUMAN
                if self.topCamState == State.BOTH:
                    self.master = Cams.TOP
            else:
                self.botCamState |= State.HUMAN
                if self.botCamState == State.BOTH:
                    self.master = Cams.BOT

            if DISPLAY_VIDEO and DRAW_ON_FRAME:
                TfPoseEstimator.draw_humans(frame, humans, imgcopy=False)
            human, midX = self.getBestFace(humans)

            if (ht.is_hands_above_head(human)):
                self.logger.debug("HANDS ABOVE HEAD!!!")

            if midX != -1:
                centerDiff = abs(midX - VideoStream.DEFAULT_WIDTH/2)
                if centerDiff > FACE_THRESHOLD:
                    if midX < VideoStream.DEFAULT_WIDTH/2:
                        # rotate CCW
                        if camera == Cams.TOP:
                            self.updateTopAngle((self.topCamAngle + centerDiff * degreePerPixel) % 360)
                        else:
                            self.updateBotAngle((self.botCamAngle + centerDiff * degreePerPixel) % 360)
                    elif midX > VideoStream.DEFAULT_WIDTH/2:
                        # rotate CW
                        if camera == Cams.TOP:
                            self.updateTopAngle((self.topCamAngle - centerDiff * degreePerPixel) % 360)
                        else:
                            self.updateBotAngle((self.botCamAngle - centerDiff * degreePerPixel) % 360)
        else:
            if camera == Cams.TOP:
                self.topCamState &= ~State.HUMAN
            else:
                self.botCamState &= ~State.HUMAN
        return frame

    def playVideo(self, cam):
        if cam == Cams.TOP:
            if self.botCamProc is not None and self.botCamProc.poll() is not None:
                self.botCamProc.kill()
            self.topCamProc = subprocess.Popen("ffmpeg -f v4l2 -i /dev/video3 -f v4l2 /dev/video5", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif cam == Cams.BOT:
            if self.topCamProc is not None and self.topCamProc.poll() is not None:
                self.topCamProc.kill()
            self.botCamProc = subprocess.Popen("ffmpeg -f v4l2 -i /dev/video4 -f v4l2 /dev/video5", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self):
        Thread(target=self.run, args=()).start()

    def run(self):
        self.stop = False
        while not self.stop:
            try:
                topFrame = self.topCamStream.read()
                botFrame = self.botCamStream.read()
                if time.time() - self.humanSampleTime > HUMAN_SAMPLE_FREQ:
                    if topFrame is not None:
                        topFrame = self.checkHumans(topFrame, Cams.TOP)
                    if botFrame is not None:
                        botFrame = self.checkHumans(botFrame, Cams.BOT)
                    self.humanSampleTime = time.time()

                if time.time() - self.micSampleTime > MIC_SAMPLE_FREQ:
                    self.checkMic()
                    self.micSampleTime = time.time()

                self.updatePositions()

                # if DISPLAY_VIDEO and topFrame is not None and botFrame is not None:
                #     if self.master == Cams.TOP:
                #         if topFrame is not None:
                #             cv.imshow('Master', topFrame)
                #         if botFrame is not None:
                #             cv.imshow('Slave', botFrame)
                #     else:
                #         if botFrame is not None:
                #             cv.imshow('Master', botFrame)
                #         if topFrame is not None:
                #             cv.imshow('Slave', topFrame)
                #     if cv.waitKey(1) == 27:
                #         pass
                if DISPLAY_VIDEO and topFrame is not None and botFrame is not None:
                    if self.master == Cams.TOP:
                        top_master = np.concatenate((topFrame, botFrame), axis=1)
                        cv.imshow('Master + Slave', top_master)
                    else:
                        bot_master = np.concatenate((botFrame, topFrame), axis=1)
                        cv.imshow('Master + Slave', bot_master)
                    if cv.waitKey(1) == 27:
                        pass

            except KeyboardInterrupt:
                self.logger.debug("Keyboard interrupt! Terminating.")
                break

        self.mc.resetMotors()
        # self.topCamStream.stop()
        # self.botCamStream.stop()
        # self.mic.close()
        # time.sleep(2)
        # cv.destroyAllWindows()

if __name__ == "__main__":
    mmnt = Moment()
    mmnt.start()
    devName = '/dev/video3'
    if not os.path.exists(devName):
        print("Warning: device does not exist", devName)
    device = open(devName, 'wb')
    mmnt.topCamStream.update()
    topFrame = mmnt.topCamStream.read()
    height, width, channels = topFrame.shape
    #Set up the formatting of our loopback device - boilerplate
    format                      = v4l2.v4l2_format()
    format.type                 = v4l2.V4L2_BUF_TYPE_VIDEO_OUTPUT
    format.fmt.pix.field        = v4l2.V4L2_FIELD_NONE
    format.fmt.pix.pixelformat  = v4l2.V4L2_PIX_FMT_BGR24
    format.fmt.pix.width        = width
    format.fmt.pix.height       = height
    format.fmt.pix.bytesperline = width * channels
    format.fmt.pix.sizeimage    = width * height * channels

    print ("set format result (0 is good):{}".format(fcntl.ioctl(device, v4l2.VIDIOC_S_FMT, format)))
    while True:
        try:
            mmnt.topCamStream.update()
            topFrame = mmnt.topCamStream.read()
            mmnt.botCamStream.update()
            botFrame = mmnt.botCamStream.read()
            if mmnt.master == Cams.TOP:
                device.write(topFrame)
            else:
                device.write(botFrame)
            concatFrame = np.concatenate((topFrame, botFrame), axis=1)
            cv.imshow('Top + Bot', concatFrame)
            if cv.waitKey(1) == 27:
                pass
        except KeyboardInterrupt:
            mmnt.mc.resetMotors()
            mmnt.topCamStream.stop()
            mmnt.botCamStream.stop()
            mmnt.mic.close()
            cv.destroyAllWindows()
            break
    device.close()
