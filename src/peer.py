import math
import sys
import os
import time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import select
import util.simsocket as simsocket
import struct
import socket
import util.bt_utils as bt_utils
import hashlib
import argparse
import pickle
import matplotlib.pyplot as plt

"""
This is CS305 project skeleton code.
Please refer to the example files - example/dumpreceiver.py and example/dumpsender.py - to learn how to play with this skeleton.
"""

BUF_SIZE = 1400
CHUNK_DATA_SIZE = 512 * 1024
HEADER_LEN = struct.calcsize("HBBHHII")
MAX_PAYLOAD = 1024

config = None
expect_output_file = None
expect_received_chunk = dict()
expect_received_chunk_flag = dict()  # 判断一个chunk是否已经发送get消息（0未发送，1发送）
as_sender_peers = dict()
as_receiver_peers = dict()

##########################
peer_has_chunks = dict()
chunk_who_has = dict()


##########################

# Packet Format
# |2byte magic|1byte team |1byte type|
# |2byte  header len  |2byte pkt len |
# |      4byte  seq                  |
# |      4byte  ack                  |

# TODO:
#  1. Handshaking & Reliable Data Transfer - Timeout(RTT) + Duplicated ACK
#  2. Congestion Control
#  3. Concurrency & Robustness

class PeerInfo_as_reciver:
    def __init__(self, peer_ip, expect_downloading_chunk_hash):
        self.peer_ip = peer_ip
        # receiver side
        self.expect_downloading_chunk_hash = expect_downloading_chunk_hash
        self.recv_list = [0 for _ in range(513)]
        self.max_continued_recv_idx = 1
        self.recv_pkt_dict = dict()
        ##########################
        self.last_connect_time = time.time()
        ##########################

        # ====================================
        # self.in_order_unsent_ack = 0
        # self.timer = 0
        # ====================================


class PeerInfo_as_sender:
    def __init__(self, peer_ip, timeout, timeout_fixed, expect_sending_chunk_hash, start_time):
        self.peer_ip = peer_ip
        # sender side
        self.timeout = timeout
        self.timeout_fixed = timeout_fixed
        self.estimateRTT = None
        self.devRTT = 0.
        self.send_list = [0 for _ in range(513)]  # 0: not send; 1: send but unACKed; 2:ACKed
        self.cwnd_head = 1
        self.window_size = 1.
        self.slowStartThresh = 64
        self.Mode = 0  # 0： slowStart; 1: congestion avoidance; 2: fast recovery
        self.seq_dupAck_list = [0 for _ in range(513)]
        self.seq_timeout_list = [0. for _ in range(513)]
        self.expect_sending_chunk_hash = expect_sending_chunk_hash
        # 收包的起始时间，画window size图用
        self.start_time = start_time
        self.last_window_size = 1
        self.last_time = 0


def process_download(sock, chunkfile, outputfile):
    '''
    if DOWNLOAD is used, the peer will keep getting files until it is done
    '''
    global expect_output_file
    global expect_received_chunk
    global expect_downloading_chunk_hash
    global expect_received_chunk_flag

    expect_output_file = outputfile
    # 1. read chunk hash to be downloaded from chunk file
    download_hash = bytes()
    with open(chunkfile, "r") as cf:
        content = cf.readlines()
        for line in content:
            index, data_hash_str = line.strip().split(" ")
            expect_received_chunk[data_hash_str] = bytes()
            expect_received_chunk_flag[data_hash_str] = 0
            ##########################
            chunk_who_has[data_hash_str] = list()
            ##########################

            # hex_str to bytes
            data_hash = bytes.fromhex(data_hash_str)
            download_hash = download_hash + data_hash

        # expect_downloading_chunk_hash = data_hash_str 这个改到握手的时候再加上

    # 2. make a WHOHAS pkt
    whohas_header = struct.pack("HBBHHII", socket.htons(52305), 35, 0, socket.htons(HEADER_LEN),
                                socket.htons(HEADER_LEN + len(download_hash)), socket.htonl(0), socket.htonl(0))
    whohas_pkt = whohas_header + download_hash

    # 3. flooding WHOHAS pkt to all peers in peer list
    peer_list = config.peers
    for peer in peer_list:
        if int(peer[0]) != config.identity:
            sock.sendto(whohas_pkt, (peer[1], int(peer[2])))


def process_inbound_udp(sock):
    # Receive pkt
    global config, expect_sending_chunk_hash

    pkt, from_addr = sock.recvfrom(BUF_SIZE)
    Magic, Team, Type, headerLen, pktLen, Seq, Ack = struct.unpack("HBBHHII", pkt[:HEADER_LEN])
    log_file = 'From: ' + str(from_addr) + '; Dst: ' + str(config.port) + '; Type: ' + str(Type) + '; Seq: ' + str(
        socket.ntohl(Seq)) + '; Ack: ' \
               + str(socket.ntohl(Ack)) + '\n'
    f = open("log_file.txt", "a")
    f.write(log_file)
    f.close()

    data = pkt[HEADER_LEN:]

    #   Type corresponding:
    #   0:WHOHAS  1:IHAVE  2:GET  3:DATA  4:ACK  5:DENIED
    if Type == 0:
        # received an WHOHAS pkt
        # 如果receiver数量达到max send,回复DENIED
        if len(as_sender_peers) >= config.max_conn:
            denied_header = struct.pack("HBBHHII", socket.htons(52305), 35, 5, socket.htons(HEADER_LEN),
                                        socket.htons(HEADER_LEN), socket.htonl(0),
                                        socket.htonl(0))
            sock.sendto(denied_header, from_addr)
        # see what chunk I have
        else:
            data_len = socket.ntohs(pktLen) - socket.ntohs(headerLen)
            chunk_num = data_len // 20
            ihave_chunk_hash = bytes()
            for i in range(chunk_num):
                request_chunk_hash = data[i * 20: i * 20 + 20]
                request_chunk_hash_str = bytes.hex(request_chunk_hash)
                if request_chunk_hash_str in config.haschunks:
                    # hex_str to bytes
                    ihave_chunk_hash = ihave_chunk_hash + request_chunk_hash

            ihave_header = struct.pack("HBBHHII", socket.htons(52305), 35, 1, socket.htons(HEADER_LEN),
                                       socket.htons(HEADER_LEN + len(ihave_chunk_hash)), socket.htonl(0),
                                       socket.htonl(0))
            ihave_pkt = ihave_header + ihave_chunk_hash
            sock.sendto(ihave_pkt, from_addr)

    elif Type == 1:
        # reveived an IHAVE pkt
        # see what chunk the sender has
        data_len = socket.ntohs(pktLen) - socket.ntohs(headerLen)
        chunk_num = data_len // 20
        ##########################1
        is_send_get = False
        he_has_chunk = list()
        ##########################1
        for i in range(chunk_num):
            get_chunk_hash = data[i * 20: i * 20 + 20]
            get_chunk_hash_str = bytes.hex(get_chunk_hash)
            ##########################2
            chunk_who_has[get_chunk_hash_str].append(from_addr)
            he_has_chunk.append(get_chunk_hash_str)
            if expect_received_chunk_flag[get_chunk_hash_str] == 0 and is_send_get == False:
                # if expect_received_chunk_flag[get_chunk_hash_str] == 0:
                # send back GET pkt
                is_send_get = True
                ##########################2
                expect_received_chunk_flag[get_chunk_hash_str] = 1
                get_header = struct.pack("HBBHHII", socket.htons(52305), 35, 2, socket.htons(HEADER_LEN),
                                         socket.htons(HEADER_LEN + len(get_chunk_hash)), socket.htonl(0),
                                         socket.htonl(0))
                get_pkt = get_header + get_chunk_hash
                sock.sendto(get_pkt, from_addr)
                # 建立连接，如果后面有denied再取消连接
                connection_info = PeerInfo_as_reciver(from_addr, get_chunk_hash_str)
                as_receiver_peers[from_addr] = connection_info
                ##########################3
                # 删掉了break
                ##########################3
                # break

        ##########################
        peer_has_chunks[from_addr] = he_has_chunk
        ##########################

    elif Type == 2:
        # received a GET pkt
        need_chunk_hash = data[:20]
        need_chunk_hash_str = bytes.hex(need_chunk_hash)

        # 检查receiver的数量，没超过的话 1.建立连接 2.发送数据
        if len(as_sender_peers) < config.max_conn:
            if config.timeout == 0:
                connection_info = PeerInfo_as_sender(from_addr, 1, False, need_chunk_hash_str, time.time())
            else:
                connection_info = PeerInfo_as_sender(from_addr, config.timeout, True, need_chunk_hash_str, time.time())
            as_sender_peers[from_addr] = connection_info

            chunk_data = config.haschunks[need_chunk_hash_str][:MAX_PAYLOAD]
            # send back data
            data_header = struct.pack("HBBHHII", socket.htons(52305), 35, 3, socket.htons(HEADER_LEN),
                                      socket.htons(HEADER_LEN + MAX_PAYLOAD), socket.htonl(1), socket.htonl(0))
            connection_info.send_list[1] = 1
            connection_info.seq_timeout_list[1] = time.time()
            sock.sendto(data_header + chunk_data, from_addr)

            ##### 开始画图
            plt.figure()
        else:
            # 头+denied的包的hash值
            denied_header = struct.pack("HBBHHII", socket.htons(52305), 35, 5, socket.htons(HEADER_LEN),
                                        socket.htons(HEADER_LEN + len(need_chunk_hash)), socket.htonl(0),
                                        socket.htonl(0))
            sock.sendto(denied_header + need_chunk_hash, from_addr)

    elif Type == 3:
        peer_info = as_receiver_peers[from_addr]
        #######################
        peer_info.last_connect_time = time.time()
        #######################
        # received an DATA pkt
        seq_num = socket.ntohl(Seq)

        peer_info.recv_list[seq_num] = 1
        peer_info.recv_pkt_dict[seq_num] = data
        for i in range(peer_info.max_continued_recv_idx, 513):
            if peer_info.recv_list[i] == 1:
                peer_info.max_continued_recv_idx = i
            else:
                break

        # =============================================================
        # if peer_info.max_continued_recv_idx == seq_num and seq_num != 512:
        #     if peer_info.in_order_unsent_ack == 0:
        #         peer_info.in_order_unsent_ack = seq_num
        #         peer_info.timer = time.time()

        #     elif seq_num == peer_info.in_order_unsent_ack + 1:
        #         #发ack包
        #         ack_pkt = struct.pack("HBBHHII", socket.htons(52305), 35, 4, socket.htons(HEADER_LEN), socket.htons(HEADER_LEN),
        #                       socket.htonl(0), socket.htonl(peer_info.max_continued_recv_idx))
        #         sock.sendto(ack_pkt, from_addr)

        #         #重置
        #         peer_info.in_order_unsent_ack = 0
        #         peer_info.timer = 0

        # send back ACK pkt
        # else:
        ack_pkt = struct.pack("HBBHHII", socket.htons(52305), 35, 4, socket.htons(HEADER_LEN), socket.htons(HEADER_LEN),
                              socket.htonl(0), socket.htonl(peer_info.max_continued_recv_idx))
        sock.sendto(ack_pkt, from_addr)

        # #重置
        # peer_info.in_order_unsent_ack = 0
        # peer_info.timer = 0

        # ==========================================================

        # see if finished
        if len(peer_info.recv_pkt_dict) == 512:
            #######################
            # finish 了要移除链接
            as_receiver_peers.pop(from_addr)
            #######################
            for i in range(1, 513):
                expect_received_chunk[peer_info.expect_downloading_chunk_hash] += peer_info.recv_pkt_dict[i]

            # add to this peer's haschunk
            config.haschunks[peer_info.expect_downloading_chunk_hash] \
                = expect_received_chunk[peer_info.expect_downloading_chunk_hash]

            # you need to print "GOT" when finished downloading all chunks in a DOWNLOAD file
            finishedAll = True
            for chunk_hash, chunk in expect_received_chunk.items():
                if len(chunk) != CHUNK_DATA_SIZE:
                    finishedAll = False
                    ##########################
                    # 1.看这个peer还有哪些chunk，并且在expect_received_chunk_flag里为0的（未发送get)
                    he_still_has = peer_has_chunks[from_addr]
                    for c in he_still_has:
                        if expect_received_chunk_flag[c] == 0:
                            # 发送get消息
                            expect_received_chunk_flag[c] = 1
                            get_chunk_hash = bytes.fromhex(c)
                            get_header = struct.pack("HBBHHII", socket.htons(52305), 35, 2, socket.htons(HEADER_LEN),
                                                     socket.htons(HEADER_LEN + len(get_chunk_hash)), socket.htonl(0),
                                                     socket.htonl(0))
                            get_pkt = get_header + get_chunk_hash
                            sock.sendto(get_pkt, from_addr)
                            # 建立新连接
                            connection_info = PeerInfo_as_reciver(from_addr, c)
                            as_receiver_peers[from_addr] = connection_info
                            break
                    ##########################
                    break
            if finishedAll:
                print(f"GOT {expect_output_file}")
                # finished downloading chunk data
                # dump the received chunk to file in dict form using pickle
                with open(expect_output_file, "wb") as wf:
                    pickle.dump(expect_received_chunk, wf)


    elif Type == 4:
        peer_info = as_sender_peers[from_addr]
        # f = open("log_file.txt", "a")
        # f.write('cwnd start: ' + str(peer_info.cwnd_head) + ' cwnd end: ' + str(peer_info.cwnd_head + math.floor(peer_info.window_size)) + '\n')
        # f.close()
        # received an ACK pkt
        ack_num = socket.ntohl(Ack)
        peer_info.seq_dupAck_list[ack_num] += 1
        if peer_info.send_list[ack_num] != 2:
            peer_info.seq_timeout_list[ack_num] = time.time() - peer_info.seq_timeout_list[ack_num]
            # compute RTT
            if ack_num % 10 == 0 and not peer_info.timeout_fixed:
                sampleRTT = peer_info.seq_timeout_list[ack_num]
                if peer_info.estimateRTT is None:
                    peer_info.estimateRTT = sampleRTT
                old_timeout = peer_info.timeout
                peer_info.estimateRTT = 0.875 * peer_info.estimateRTT + 0.125 * sampleRTT
                peer_info.devRTT = 0.75 * peer_info.devRTT + 0.25 * math.fabs(peer_info.estimateRTT - sampleRTT)
                peer_info.timeout = peer_info.estimateRTT + 4 * peer_info.devRTT
                # f = open("log_file.txt", "a")
                # f.write('Old Timeout: ' + str(old_timeout) + '; SampleRTT: ' + str(sampleRTT) + '; EstimatedRTT: ' +
                #         str(peer_info.estimateRTT) + '; DevRTT: ' + str(peer_info.devRTT) + '; New Timeout: ' + str(peer_info.timeout) + '\n')
                # f.close()

        peer_info.send_list[ack_num] = 2

        # 任意阶段收到3 duplicate Ack，进入fast recovery
        if peer_info.seq_dupAck_list[ack_num] == 2:
            pass
        elif peer_info.seq_dupAck_list[ack_num] == 3:
            # Fast Transmission
            left = (ack_num) * MAX_PAYLOAD
            right = min((ack_num + 1) * MAX_PAYLOAD, CHUNK_DATA_SIZE)
            # [left: right] is the next part of chunk
            next_data = config.haschunks[peer_info.expect_sending_chunk_hash][left: right]
            # change thresh
            peer_info.slowStartThresh = max(math.floor(peer_info.window_size / 2), 2)
            peer_info.window_size = float(peer_info.slowStartThresh + 3)  # fast recovery, 是否需要判断越界？
            peer_info.Mode = 2  # 进入fast recovery
            # send next data
            data_header = struct.pack("HBBHHII", socket.htons(52305), 35, 3, socket.htons(HEADER_LEN),
                                      socket.htons(HEADER_LEN + len(next_data)), socket.htonl(ack_num + 1),
                                      socket.htonl(0))
            # peer_info.seq_timeout_list[ack_num + 1] = time.time()
            peer_info.send_list[ack_num + 1] = 1
            sock.sendto(data_header + next_data, from_addr)
        # fast recovery阶段每收到一个duplicate ACK, window size就加一 （Week8 ppt P45）
        elif peer_info.Mode == 2 and peer_info.seq_dupAck_list[ack_num] > 3:
            peer_info.window_size += 1
            if (ack_num + math.floor(peer_info.window_size)) <= 512:  # 判断是否越界
                if peer_info.send_list[ack_num + math.floor(peer_info.window_size)] == 0:
                    # ???
                    left = (ack_num + math.floor(peer_info.window_size) - 1) * MAX_PAYLOAD
                    right = min((ack_num + math.floor(peer_info.window_size)) * MAX_PAYLOAD, CHUNK_DATA_SIZE)
                    # [left: right] is the next part of chunk
                    next_data = config.haschunks[peer_info.expect_sending_chunk_hash][left: right]
                    # send next data
                    data_header = struct.pack("HBBHHII", socket.htons(52305), 35, 3, socket.htons(HEADER_LEN),
                                              socket.htons(HEADER_LEN + len(next_data)),
                                              socket.htonl(ack_num + math.floor(peer_info.window_size)),
                                              socket.htonl(0))
                    peer_info.send_list[ack_num + math.floor(peer_info.window_size)] = 1
                    peer_info.seq_timeout_list[ack_num + math.floor(peer_info.window_size)] = time.time()
                    sock.sendto(data_header + next_data, from_addr)
                    # f = open("log_file.txt", "a")
                    # f.write('enter fast recovery send' + 'Seq: ' + str(ack_num + math.floor(peer_info.window_size)) + '\n')
                    # f.close()
            else:
                # 越界还原window size
                peer_info.window_size -= 1
        # receive new Ack & 小于3的dup Ack
        else:

            if ack_num * MAX_PAYLOAD >= CHUNK_DATA_SIZE:
                # finished
                ###################################
                as_sender_peers.pop(from_addr)
                ###################################
                pass
            else:
                # 先判断现在是哪个Mode
                # change window size
                if peer_info.Mode == 0:  # slowStart
                    peer_info.window_size += 1
                    if peer_info.window_size >= peer_info.slowStartThresh:
                        peer_info.Mode = 1  # enter the congestion control
                elif peer_info.Mode == 2:  # 当前处于fast recovery, 但收到了new Ack,要进入congestion control状态
                    peer_info.window_size = peer_info.slowStartThresh
                    peer_info.Mode = 1
                else:
                    peer_info.window_size += 1 / peer_info.window_size
                # slide the window
                for i in range(peer_info.cwnd_head, ack_num + 1):
                    peer_info.send_list[i] = 2
                peer_info.cwnd_head = ack_num + 1
                # send pkt in cwnd
                for i in range(math.floor(peer_info.window_size)):
                    seq_num = peer_info.cwnd_head + i
                    if seq_num > 512:
                        break
                    if peer_info.send_list[seq_num] == 0:
                        left = (seq_num - 1) * MAX_PAYLOAD
                        right = min(seq_num * MAX_PAYLOAD, CHUNK_DATA_SIZE)
                        # [left: right] is the next part of chunk
                        next_data = config.haschunks[peer_info.expect_sending_chunk_hash][left: right]
                        # send next data
                        data_header = struct.pack("HBBHHII", socket.htons(52305), 35, 3, socket.htons(HEADER_LEN),
                                                  socket.htons(HEADER_LEN + len(next_data)), socket.htonl(seq_num),
                                                  socket.htonl(0))
                        peer_info.seq_timeout_list[seq_num] = time.time()
                        peer_info.send_list[seq_num] = 1
                        sock.sendto(data_header + next_data, from_addr)

        # 在图上画点
        # interval_time = time.time() - peer_info.start_time
        # plt.plot(interval_time, peer_info.window_size, ",", markersize=1.)
        now_time = time.time() - peer_info.start_time
        x_ = [peer_info.last_time, now_time]
        y_ = [peer_info.last_window_size, peer_info.window_size]
        plt.plot(x_, y_, 'b-')
        peer_info.last_time = now_time
        peer_info.last_window_size = peer_info.window_size
        if (ack_num == 512):
            plt.xlabel("Time")
            plt.ylabel("Window Size")
            plt.savefig("window_size.png")
            #  plt.show()

    elif Type == 5:
        if len(data) > 0:
            # 重设expect_received_chunk_flag为0
            # 清除连接
            refuse_chunk_hash = data[:20]
            refuse_chunk_hash_str = bytes.hex(refuse_chunk_hash)
            expect_received_chunk_flag[refuse_chunk_hash_str] = 0
            as_receiver_peers.pop(from_addr)


def process_user_input(sock):
    command, chunkf, outf = input().split(' ')
    if command == 'DOWNLOAD':
        process_download(sock, chunkf, outf)
    else:
        pass


def checkTimeout(sock):
    for from_addr, peer in as_sender_peers.items():
        for i in range(math.floor(peer.window_size)):
            if peer.cwnd_head + i > 512:
                break
            if peer.send_list[peer.cwnd_head + i] == 1:
                duration = time.time() - peer.seq_timeout_list[peer.cwnd_head + i]
                if duration > peer.timeout:
                    peer.timeout *= 2
                    info = 'From: ' + str(config.port) + '; Dst: ' + str(from_addr) + '; Seq: ' + str(
                        peer.cwnd_head + i) + '; Duration: ' + str(duration) + '; Timeout:' + str(
                        peer.timeout) + '\n'
                    f = open("log_file.txt", "a")
                    f.write(info)
                    f.close()
                    # change state
                    peer.window_size = 1.
                    peer.slowStartThresh = max(math.floor(peer.window_size / 2), 2)
                    if peer.Mode != 0:
                        peer.Mode = 0

                    left = (peer.cwnd_head + i - 1) * MAX_PAYLOAD
                    right = min((peer.cwnd_head + i) * MAX_PAYLOAD, CHUNK_DATA_SIZE)
                    # [left: right] is the next part of chunk
                    next_data = config.haschunks[peer.expect_sending_chunk_hash][left: right]
                    # send next data
                    data_header = struct.pack("HBBHHII", socket.htons(52305), 35, 3, socket.htons(HEADER_LEN),
                                              socket.htons(HEADER_LEN + len(next_data)),
                                              socket.htonl(peer.cwnd_head + i),
                                              socket.htonl(0))
                    peer.seq_timeout_list[peer.cwnd_head + i] = time.time()
                    sock.sendto(data_header + next_data, from_addr)


def check_crush(sock):
    pop_list = list()
    for peer, info in as_receiver_peers.items():
        if time.time() - info.last_connect_time > 10:
            pop_list.append((peer, info))
    for peer, info in pop_list:
        chunk_str = info.expect_downloading_chunk_hash
        expect_received_chunk_flag[chunk_str] = 0
        expect_received_chunk[chunk_str] = bytes()

        as_receiver_peers.pop(peer)
        peer_has_chunks.pop(peer)
        # 2.对这个包再get
        peers_has_this_chunk = chunk_who_has[chunk_str]
        for p in peers_has_this_chunk:
            if p != peer and p not in as_receiver_peers.keys():
                # 发送get消息
                expect_received_chunk_flag[chunk_str] = 1
                get_chunk_hash = bytes.fromhex(chunk_str)
                get_header = struct.pack("HBBHHII", socket.htons(52305), 35, 2, socket.htons(HEADER_LEN),
                                         socket.htons(HEADER_LEN + len(get_chunk_hash)), socket.htonl(0),
                                         socket.htonl(0))
                get_pkt = get_header + get_chunk_hash
                sock.sendto(get_pkt, p)
                # 建立新连接
                connection_info = PeerInfo_as_reciver(p, chunk_str)
                as_receiver_peers[p] = connection_info
                break


# =====================================================================
def check_delay_ack(sock):
    for peer, info in as_receiver_peers.items():
        # 0.02可调整
        if info.timer != 0 and time.time() - info.timer > 0.007:
            ack_pkt = struct.pack("HBBHHII", socket.htons(52305), 35, 4, socket.htons(HEADER_LEN),
                                  socket.htons(HEADER_LEN),
                                  socket.htonl(0), socket.htonl(info.in_order_unsent_ack))
            # 重置
            sock.sendto(ack_pkt, peer)
            info.in_order_unsent_ack = 0
            info.timer = 0


# =====================================================================


def peer_run(config):
    addr = (config.ip, config.port)
    sock = simsocket.SimSocket(config.identity, addr, verbose=config.verbose)

    try:
        while True:
            ready = select.select([sock, sys.stdin], [], [], 0.1)
            read_ready = ready[0]

            check_crush(sock)

            # ===========================
            # check_delay_ack(sock)
            # ===========================

            if len(read_ready) > 0:
                if sock in read_ready:
                    process_inbound_udp(sock)

                if sys.stdin in read_ready:
                    process_user_input(sock)

            checkTimeout(sock)

            # else:
            #     # No pkt nor input arrives during this period
            #     pass
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == '__main__':
    """
    -p: Peer list file, it will be in the form "*.map" like nodes.map.
    -c: Chunkfile, a dictionary dumped by pickle. It will be loaded automatically in bt_utils. The loaded dictionary has the form: {chunkhash: chunkdata}
    -m: The max number of peer that you can send chunk to concurrently. If more peers ask you for chunks, you should reply "DENIED"
    -i: ID, it is the index in nodes.map
    -v: verbose level for printing logs to stdout, 0 for no verbose, 1 for WARNING level, 2 for INFO, 3 for DEBUG.
    -t: pre-defined timeout. If it is not set, you should estimate timeout via RTT. If it is set, you should not change this time out.
        The timeout will be set when running test scripts. PLEASE do not change timeout if it set.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', type=str, help='<peerfile>     The list of all peers', default='nodes.map')
    parser.add_argument('-c', type=str, help='<chunkfile>    Pickle dumped dictionary {chunkhash: chunkdata}')
    parser.add_argument('-m', type=int, help='<maxconn>      Max # of concurrent sending')
    parser.add_argument('-i', type=int, help='<identity>     Which peer # am I?')
    parser.add_argument('-v', type=int, help='verbose level', default=0)
    parser.add_argument('-t', type=int, help="pre-defined timeout", default=0)
    args = parser.parse_args()

    config = bt_utils.BtConfig(args)
    peer_run(config)
