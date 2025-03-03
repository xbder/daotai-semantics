import sys
import time
import socket
import traceback
from utils.pyKinectUtil import Kinect
import numpy as np
from PIL import Image, ImageFont, ImageDraw

import face_recognition
from config import *
from utils.commonutil import getFormatTime, crop_face, is_effective
from utils.dbUtil import saveMyComing2DB
from utils.dateUtil import formatTimestamp
import configparser
import pika
from utils.CapUtil import Stack
import threading
from keras.utils.data_utils import get_file
from wide_resnet import WideResNet


'''
    来人感知模块
    Note：获取Kinect画面
    Note：自定义Stack(stack_szie)，解决消费速度跟不上生成速度的情况；
    Note：图像帧积压过多会报错：[h264 @ 0000000000498f40] error while decoding MB 8 21, bytestream -13
'''

frame_buffer = Stack(30 * 5)
lock = threading.RLock()

# 发送来人消息
def send_comming(commingDict):
    # 读取配置文件并创建rabbit producer
    nodeName = "rabbit2backstage"  # 读取该节点的数据
    cf = configparser.ConfigParser()
    cf.read("./kdata/config.conf")
    host = str(cf.get(nodeName, "host"))
    port = int(cf.get(nodeName, "port"))
    username = str(cf.get(nodeName, "username"))
    password = str(cf.get(nodeName, "password"))
    backstage_EXCHANGE_NAME = str(cf.get(nodeName, "EXCHANGE_NAME"))
    vhost = str(cf.get(nodeName, "vhost"))
    backstage_routingKey = str(cf.get(nodeName, "routingKey"))
    backstage_queueName = str(cf.get(nodeName, "QUEUE_NAME"))

    credentials = pika.PlainCredentials(username=username, password=password)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=host, port=port, heartbeat=0, virtual_host=vhost, credentials=credentials))
    connection.process_data_events()  # 防止主进程长时间等待，而导致rabbitmq主动断开连接，所以要定期发心跳调用
    backstage_channel = connection.channel()

    # 以下原来是在消费者里面
    backstage_channel.exchange_declare(exchange=backstage_EXCHANGE_NAME,
                                       exchange_type='direct')  # 声明交换机
    backstage_channel.queue_declare(queue=backstage_queueName)  # 声明队列。消费者需要这样代码，生产者不需要
    backstage_channel.queue_bind(queue=backstage_queueName, exchange=backstage_EXCHANGE_NAME,
                                 routing_key=backstage_routingKey)  # 绑定队列和交换机

    backstage_channel.basic_publish(exchange=backstage_EXCHANGE_NAME,
                                    routing_key=backstage_routingKey,
                                    body=str(commingDict))  # 将语义识别结果给到后端
    connection.close()



# # 到backstage的心跳机制
# # 手动做心跳机制，避免rabbit server自动断开连接。。自动发心跳机制存在的问题：因rannitmq有流量控制，会屏蔽掉自动心跳机制
# def mycoming_heartbeat():
#     heartbeatDict = {}
#     heartbeatDict["daotaiID"] = daotaiID
#     heartbeatDict["sentences"] = ""
#     heartbeatDict["timestamp"] = str(int(time.time() * 1000))
#     heartbeatDict["intention"] = "heartbeat"  # 心跳
#
#     backstage_channel.basic_publish(exchange=backstage_EXCHANGE_NAME,
#                                    routing_key=backstage_routingKey,
#                                    body=str(heartbeatDict))
#     # print("heartbeatDict:", heartbeatDict)
#     global timer_mycoming
#     timer_mycoming = threading.Timer(3, mycoming_heartbeat)
#     timer_mycoming.start()

def Receive():
    print("start Receive")

    kinect = Kinect()
    while True:
        # color_data = kinect.get_the_data_of_color_depth_infrared_image()  # 获得最新的彩色和深度图像以及红外图像
        color_data = kinect.get_the_data_of_color()    # 只获取最新的色彩图
        if color_data[0] is not None:
            lock.acquire()
            frame_buffer.push(color_data[0])
            lock.release()

def percept():
    # comming_log = Logger('D:/data/daotai_comming.log', level='info')
    # comming_mq_log = Logger('D:/data/daotai_comming_mq.log', level='info')

    # 人脸检测
    global face_detect    # 子线程里加载模型，需要将模型指定成全局变量
    face_detect = face_recognition.FaceDetection()  # 初始化mtcnn

    print("face_detect:", face_detect)
    # comming_log.logger.info("face_detect: %s" % (face_detect))

    # 性别年龄识别模型
    global age_gender_model
    age_gender_model = WideResNet(face_size, depth=16, k=8)()
    age_gender_model.load_weights("./model_data/weights.18-4.06.hdf5")

    while True:
        if frame_buffer.size() > 0:
            lock.acquire()
            frame = frame_buffer.pop()    # 每次拿最新的
            frame_buffer.clear()    # 每次拿之后清空缓冲区
            lock.release()

            frame = np.array(frame)
            # print("frame:", type(frame), frame.shape)    # numpy.ndarray, (1080, 1920, 3)
            height, width, channel = frame.shape
            bboxes, landmarks = face_detect.detect_face(frame)
            bboxes, landmarks = face_detect.get_square_bboxes(bboxes, landmarks, fixed="height")  # 以高为基准，获得等宽的矩形
            if bboxes == [] or landmarks == []:
                pass
            else:
                print("1、faces.faceNum:", len(bboxes))
                # comming_log.logger.info("faces.faceNum: %s" % (len(bboxes)))
                box_areas = []
                for i in range(0, len(bboxes)):
                    box = bboxes[i]
                    left, top, right, bottom = box
                    w = right - left
                    h = bottom - top
                    box_areas.append(w * h)    # 人头的面积

                # 找最大的人脸及坐标
                max_face_area = max(box_areas)    # 最大的人脸面积
                max_face_box = bboxes[box_areas.index(max_face_area)]    # 最大人脸面积框对应的坐标
                # print("max_face_area: %s, max_face_box: %s" % (max_face_area, max_face_box))
                # comming_log.logger.info("max_face_area: %s, max_face_box: %s" % (max_face_area, max_face_box))
                if max_face_area > face_area_threshold and is_effective(max_face_box, height, width):    # 判断人脸框面积大于阀值 and 在有效识别区内
                    # print("mtcnn-bboxes--> ", bboxes)
                    # print("mtcnn-landmarks--> ", landmarks)
                    print("2、人大小符合要求：面积：%d" % (max_face_area))
                    # 这里新增来人的性别年龄识别
                    left, top, right, bottom = max_face_box

                    image = Image.fromarray(frame)

                    # 2.性别年龄检测
                    tmp = crop_face(image, box, margin=40,
                                    size=face_size)  # 裁剪脑袋部分，并resize，image：<class 'PIL.Image.Image'>
                    faces = [[left, top, right, bottom]]  # 做成需要的格式：[[], [], []]
                    face_imgs = np.empty((len(faces), face_size, face_size, 3))
                    # face_imgs[0, :, :, :] = cv2.resize(np.asarray(tmp), (face_size, face_size))    # PIL.Image转为np.ndarray，不resize会报错：ValueError: could not broadcast input array from shape (165,165,3) into shape (64,64,3)
                    face_imgs[0, :, :, :] = tmp
                    # print("face_imgs:", type(face_imgs), face_imgs.shape)

                    results = age_gender_model.predict(face_imgs)  # 性别年龄识别
                    predicted_genders = results[0]
                    ages = np.arange(0, 101).reshape(101, 1)
                    predicted_ages = results[1].dot(ages).flatten()

                    gender = "F" if predicted_genders[0][0] > 0.5 else "M"  # 性别初筛
                    gender_ratio = max(predicted_genders[0])  # 性别概率

                    if gender_ratio < gender_ratio_threshold:    # 低于性别阀值，直接说 乘客
                        gender = "O"

                    age = int(predicted_ages[0])

                    commingDict = {}
                    commingDict["daotaiID"] = daotaiID
                    commingDict["sentences"] = "%s,%s,%s,%s,%s,%s,%s,%s,%s" % (gender, str(age), str(left), str(top), str(right), str(bottom), str(face_area_threshold), str(height), str(width))    # sentences字段填性别、年龄、位置（左上右下），逗号隔开
                    commingDict["timestamp"] = str(int(time.time() * 1000))
                    commingDict["intention"] = "mycoming"  # 表示有人来了

                    # print("commingDict: %s" % (commingDict))
                    # comming_log.logger.info("commingDict: %s" % (commingDict))
                    send_comming(str(commingDict))
                    print("已写入消息队列-commingDict: %s" % str(commingDict))
                    # comming_mq_log.logger.info("已写入消息队列-commingDict: %s" % str(commingDict))
                    saveMyComing2DB(commingDict)

                    # 这里保存下标注过的图片
                    cv2.rectangle(frame, (left, top), (right, bottom), (0, 0, 255), 2)  # 红色框，BGR

                    showInfo = "%s %s" % (gender, str(age))  # 性别，年龄
                    # print("showInfo:", showInfo)
                    t_size = (10 * len(showInfo), 22)
                    c2 = left + t_size[0], top - t_size[1] - 3  # 纵坐标，多减3目的是字上方稍留空
                    cv2.rectangle(frame, (left, top), c2, (255, 0, 0), -1)  # filled，蓝色填充，BGR
                    # print("t_size:", t_size, " c1:", c1, " c2:", c2)

                    # Draw a label with a name below the face
                    # cv2.rectangle(im0, c1, c2, (0, 0, 255), cv2.FILLED)
                    font = cv2.FONT_HERSHEY_DUPLEX

                    # 将CV2转为PIL，添加中文label后再转回来
                    pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    draw = ImageDraw.Draw(pil_img)
                    font = ImageFont.truetype('simhei.ttf', 20, encoding='utf-8')
                    draw.text((left, top - 20), showInfo, (255, 255, 255), font=font)

                    frame = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)  # PIL转CV2

                    curr_time = formatTimestamp(time.time(), format="%Y%m%d_%H%M%S", ms=True)
                    # 保存
                    savefile = "D:/data/coming/" + curr_time + ".jpg"
                    cv2.imwrite(savefile, frame)

            if height != 480 or width != 640:
                frame = cv2.resize(frame, (640, 480))    # resize时的顺序为：宽，高
            cv2.imshow("frame", frame)
            cv2.waitKey(1)

if __name__ == '__main__':
    # p_heartbeat = threading.Timer(3, mycoming_heartbeat)
    # p_heartbeat.start()
    p1 = threading.Thread(target=Receive)
    p2 = threading.Thread(target=percept)
    p1.start()
    time.sleep(5)
    p2.start()