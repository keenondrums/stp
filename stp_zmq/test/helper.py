import os
import types
from distutils.dir_util import copy_tree

from copy import deepcopy

from stp_core.common.util import adict
from stp_core.loop.eventually import eventually
from stp_core.network.port_dispenser import genHa
from stp_core.test.helper import Printer, prepStacks, chkPrinted

from stp_zmq.util import generate_certificates
from stp_zmq.zstack import ZStack


def genKeys(baseDir, names):
    generate_certificates(baseDir, *names, clean=True)
    for n in names:
        d = os.path.join(baseDir, n)
        os.makedirs(d, exist_ok=True)
        for kd in ZStack.keyDirNames():
            copy_tree(os.path.join(baseDir, kd), os.path.join(d, kd))


def add_counters_to_ping_pong(stack):
    stack.sent_ping_count = 0
    stack.sent_pong_count = 0
    stack.recv_ping_count = 0
    stack.recv_pong_count = 0
    orig_send_method = stack.sendPingPong
    orig_recv_method = stack.handlePingPong

    def send_ping_pong_counter(self, remote, is_ping=True):
        if is_ping:
            self.sent_ping_count += 1
        else:
            self.sent_pong_count += 1

        return orig_send_method(remote, is_ping)

    def recv_ping_pong_counter(self, msg, frm, ident):
        if msg in (self.pingMessage, self.pongMessage):
            if msg == self.pingMessage:
                self.recv_ping_count += 1
            if msg == self.pongMessage:
                self.recv_pong_count += 1

        return orig_recv_method(msg, frm, ident)

    stack.sendPingPong = types.MethodType(send_ping_pong_counter, stack)
    stack.handlePingPong = types.MethodType(recv_ping_pong_counter, stack)


def create_and_prep_stacks(names, basedir, looper, conf):
    genKeys(basedir, names)
    printers = [Printer(n) for n in names]
    # adict is used below to copy the config module since one stack might
    # have different config from others
    stacks = [ZStack(n, ha=genHa(), basedirpath=basedir,
                     msgHandler=printers[i].print,
                     restricted=True, config=adict(**conf.__dict__))
              for i, n in enumerate(names)]
    prepStacks(looper, *stacks, connect=True, useKeys=True)
    return stacks, printers


def check_stacks_communicating(looper, stacks, printers):
    """
    Check that `stacks` are able to send and receive messages to each other
    Assumes for each stack in `stacks`, there is a printer in `printers`,
    at the same index
    """

    # Each sends the same message to all other stacks
    for idx, stack in enumerate(stacks):
        for other_stack in stacks:
            if stack != other_stack:
                stack.send({'greetings': '{} here'.format(stack.name)},
                           other_stack.name)

    # Each stack receives message from others
    for idx, printer in enumerate(printers):
        for j, stack in enumerate(stacks):
            if idx != j:
                looper.run(eventually(chkPrinted, printer,
                                      {'greetings': '{} here'.format(stack.name)}))
