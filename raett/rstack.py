import sys
import time
from collections import Callable
from typing import Any, Set, Optional
from typing import Dict
from typing import Tuple

from raett.raeting import AutoMode, TrnsKind
from raett.road.estating import RemoteEstate
from raett.road.keeping import RoadKeep
from raett.road.stacking import RoadStack
from raett.road.transacting import Joiner, Allower, Messenger
from stp_core.common.error import error
from stp_core.common.log import getlogger

from stp_core.crypto.util import ed25519SkToCurve25519, \
    getEd25519AndCurve25519Keys
from stp_core.network.network_interface import NetworkInterface
from stp_core.network.util import checkPortAvailable, distributedConnectionMap
from stp_core.ratchet import Ratchet
from stp_core.types import HA

logger = getlogger()

# this overrides the defaults
Joiner.RedoTimeoutMin = 1.0
Joiner.RedoTimeoutMax = 10.0

Allower.RedoTimeoutMin = 1.0
Allower.RedoTimeoutMax = 10.0

Messenger.RedoTimeoutMin = 1.0
Messenger.RedoTimeoutMax = 10.0


class RStack(NetworkInterface):
    def __init__(self, *args, **kwargs):

        checkPortAvailable(kwargs['ha'])
        basedirpath = kwargs.get('basedirpath')
        keep = RoadKeep(basedirpath=basedirpath,
                        stackname=kwargs['name'],
                        auto=kwargs.get('auto'),
                        baseroledirpath=basedirpath)  # type: RoadKeep
        kwargs['keep'] = keep
        localRoleData = keep.loadLocalRoleData()

        sighex = kwargs.pop('sighex', None) or localRoleData['sighex']
        if not sighex:
            (sighex, _), (prihex, _) = getEd25519AndCurve25519Keys()
        else:
            prihex = ed25519SkToCurve25519(sighex, toHex=True)
        kwargs['sigkey'] = sighex
        kwargs['prikey'] = prihex

        self.msgHandler = kwargs.pop('msgHandler', None)  # type: Callable
        # if no timeout is set then message will never timeout
        self.messageTimeout = kwargs.pop('messageTimeout', 0)

        self.raetStack = RoadStack(self, *args, **kwargs)

        if self.ha[1] != kwargs['ha'].port:
            error("the stack port number has changed, likely due to "
                  "information in the keep. {} passed {}, actual {}".
                  format(kwargs['name'], kwargs['ha'].port, self.ha[1]))
        self._created = time.perf_counter()
        self.coro = None

        self._conns = set()  # type: Set[str]

    def __repr__(self):
        return self.name

    @property
    def name(self):
        return self.raetStack.name

    @property
    def remotes(self):
        return self.raetStack.remotes

    @property
    def created(self):
        return self._created

    @staticmethod
    def isRemoteConnected(r) -> bool:
        """
        A node is considered to be connected if it is joined, allowed and alived.

        :param r: the remote to check
        """
        return r.joined and r.allowed and r.alived

    def removeRemote(self, r):
        self.raetStack.removeRemote(r)

    def transmit(self, msg, uid, timeout=None):
        self.raetStack.transmit(msg, uid, timeout=timeout)

    @property
    def ha(self):
        return self.raetStack.ha

    def start(self):
        if not self.opened:
            self.open()
        logger.info("stack {} starting at {} in {} mode"
                    .format(self, self.ha, self.raetStack.keep.auto.name),
                    extra={"cli": False})
        # self.coro = self._raetcoro()
        self.coro = self._raetcoro

    def stop(self):
        if self.opened:
            self.close()
        self.coro = None
        logger.info("stack {} stopped".format(self.name), extra={"cli": False})

    async def service(self, limit=None) -> int:
        """
        Service `limit` number of received messages in this stack.

        :param limit: the maximum number of messages to be processed. If None,
        processes all of the messages in rxMsgs.
        :return: the number of messages processed.
        """
        pracLimit = limit if limit else sys.maxsize
        if self.coro:
            # x = next(self.coro)
            x = await self.coro()
            if x > 0:
                for x in range(pracLimit):
                    try:
                        self.msgHandler(self.raetStack.rxMsgs.popleft())
                    except IndexError:
                        break
            return x
        else:
            logger.debug("{} is stopped".format(self))
            return 0

    # def _raetcoro(self):
    #     """
    #     Generator to service all messages.
    #     Yields the length of rxMsgs queue of this stack.
    #     """
    #     while True:
    #         try:
    #             self._serviceStack(self.age)
    #             l = len(self.rxMsgs)
    #         except Exception as ex:
    #             if isinstance(ex, OSError) and \
    #                     len(ex.args) > 0 and \
    #                     ex.args[0] == 22:
    #                 logger.error("Error servicing stack {}: {}. This could be "
    #                              "due to binding to an internal network "
    #                              "and trying to route to an external one.".
    #                              format(self.name, ex), extra={'cli': 'WARNING'})
    #             else:
    #                 logger.error("Error servicing stack {}: {} {}".
    #                              format(self.name, ex, ex.args),
    #                              extra={'cli': 'WARNING'})
    #
    #             l = 0
    #         yield l

    async def _raetcoro(self):
        try:
            await self._serviceStack(self.age)
            l = len(self.raetStack.rxMsgs)
        except Exception as ex:
            if isinstance(ex, OSError) and \
                    len(ex.args) > 0 and \
                    ex.args[0] == 22:
                logger.error("Error servicing stack {}: {}. This could be "
                             "due to binding to an internal network "
                             "and trying to route to an external one.".
                             format(self.name, ex), extra={'cli': 'WARNING'})
            else:
                logger.error("Error servicing stack {}: {} {}".
                             format(self.name, ex, ex.args),
                             extra={'cli': 'WARNING'})

            l = 0
        return l

    async def _serviceStack(self, age):
        """
        Update stacks clock and service all tx and rx messages.

        :param age: update timestamp of this RoadStack to this value
        """
        self.updateStamp(age)
        self.raetStack.serviceAll()

    def updateStamp(self, age=None):
        """
        Change the timestamp of this stack's test store.

        :param age: the timestamp will be set to this value
        """
        self.raetStack.store.changeStamp(age if age else self.age)

    @property
    def opened(self):
        return self.raetStack.server.opened

    def open(self):
        """
        Open the UDP socket of this stack's server.
        """
        self.raetStack.server.open()  # close the UDP socket

    def close(self):
        """
        Close the UDP socket of this stack's server.
        """
        self.raetStack.server.close()  # close the UDP socket

    @property
    def isKeySharing(self):
        return self.raetStack.keep.auto != AutoMode.never

    @property
    def verhex(self):
        return self.raetStack.local.signer.verhex

    @property
    def keyhex(self):
        return self.raetStack.local.signer.keyhex

    @property
    def pubhex(self):
        return self.raetStack.local.priver.pubhex

    @property
    def prihex(self):
        return self.raetStack.local.priver.keyhex

    def send(self, msg: Any, remoteName: str):
        """
        Transmit the specified message to the remote specified by `remoteName`.

        :param msg: a message
        :param remoteName: the name of the remote
        """
        rid = self.getRemote(remoteName).uid
        # Setting timeout to never expire
        self.raetStack.transmit(msg, rid, timeout=self.messageTimeout)


class SimpleRStack(RStack):
    def __init__(self, stackParams: Dict, msgHandler: Callable, messageTimeout=None, sighex: str=None):
        self.stackParams = stackParams
        self.msgHandler = msgHandler
        super().__init__(**stackParams, msgHandler=self.msgHandler, messageTimeout=messageTimeout, sighex=sighex)

    def start(self):
        super().start()


class KITRStack(SimpleRStack):
    # Keep In Touch RStack. RStack which maintains connections mentioned in
    # its registry
    def __init__(self, stackParams: dict, msgHandler: Callable,
                 registry: Dict[str, HA], sighex: str=None):
        self.registry = registry

        super().__init__(stackParams, msgHandler, sighex)
        # self.bootstrapped = False

        self.lastcheck = {}  # type: Dict[int, Tuple[int, float]]
        self.ratchet = Ratchet(a=8, b=0.198, c=-4, base=8, peak=3600)

        # holds the last time we checked remotes
        self.nextCheck = 0

        # courteous bi-directional joins
        self.connectNicelyUntil = None

        self.reconnectToMissingIn = 6
        self.reconnectToDisconnectedIn = 6

    def start(self):
        super().start()
        if self.name in self.registry:
            # remove this node's registration from the  Registry
            # (no need to connect to itself)
            del self.registry[self.name]

    def serviceLifecycle(self) -> None:
        """
        Function that does the following activities if the node is going:
        (See `Status.going`)

        - check connections (See `checkConns`)
        - maintain connections (See `maintainConnections`)
        """
        self.checkConns()
        self.maintainConnections()

    def addRemote(self, remote, dump=False):
        if not self.findInNodeRegByHA(remote.ha):
            logger.debug('Remote {} with HA {} not added -> not found in registry'.format(remote.name, remote.ha))
            return
        return super(KITRStack, self).addRemote(remote, dump)

    def createRemote(self, ha):
        if ha and not self.findInNodeRegByHA(ha):
            logger.debug('Remote with HA {} not added -> not found in registry'.format(ha))
            return
        return super(KITRStack, self).createRemote(ha)

    def processRx(self, packet):
        # Override to add check that in case of join new remote is in registry. This is done to avoid creation
        # of unnecessary JSON files for remotes
        tk = packet.data['tk']

        if tk in [TrnsKind.join]: # join transaction
            sha = (packet.data['sh'],  packet.data['sp'])
            if not self.findInNodeRegByHA(sha):
                return self.handleJoinFromUnregisteredRemote(sha)

        return super(KITRStack, self).processRx(packet)

    def handleJoinFromUnregisteredRemote(self, sha):
        logger.debug('Remote with HA {} not added -> not found in registry'.format(sha))
        return None

    def connect(self, name, rid: Optional[int]=None) -> Optional[int]:
        """
        Connect to the node specified by name.

        :param name: name of the node to connect to
        :type name: str or (HA, tuple)
        :return: the uid of the remote estate, or None if a connect is not
            attempted
        """
        # if not self.isKeySharing:
        #     logger.debug("{} skipping join with {} because not key sharing".
        #                   format(self, name))
        #     return None
        if rid:
            remote = self.remotes[rid]
        else:
            if isinstance(name, (HA, tuple)):
                node_ha = name
            elif isinstance(name, str):
                node_ha = self.registry[name]
            else:
                raise AttributeError()

            remote = RemoteEstate(stack=self,
                                  ha=node_ha)
            self.addRemote(remote)
        # updates the store time so the join timer is accurate
        self.updateStamp()
        self.raetStack.join(uid=remote.uid, cascade=True, timeout=30)
        logger.info("{} looking for {} at {}:{}".
                    format(self, name or remote.name, *remote.ha),
                    extra={"cli": "PLAIN", "tags": ["node-looking"]})
        return remote.uid

    @property
    def notConnectedNodes(self) -> Set[str]:
        """
        Returns the names of nodes in the registry this node is NOT connected
        to.
        """
        return set(self.registry.keys()) - self.conns

    def maintainConnections(self, force=False):
        """
        Ensure appropriate connections.

        """
        cur = time.perf_counter()
        if cur > self.nextCheck or force:

            self.nextCheck = cur + (6 if self.isKeySharing else 15)
            # check again in 15 seconds,
            # unless sooner because of retries below

            conns, disconns = self.remotesByConnected()

            for disconn in disconns:
                self.handleDisconnectedRemote(cur, disconn)

            # remove items that have been connected
            for connected in conns:
                self.lastcheck.pop(connected.uid, None)

            self.connectToMissing(cur)

            logger.debug("{} next check for retries in {:.2f} seconds".
                         format(self, self.nextCheck - cur))
            return True
        return False

    def connectToMissing(self, currentTime):
        """
        Try to connect to the missing node within the time specified by
        `reconnectToMissingIn`

        :param currentTime: the current time
        """
        missing = self.reconcileNodeReg()
        if missing:
            logger.debug("{} found the following missing connections: {}".
                         format(self, ", ".join(missing)))
            if self.connectNicelyUntil is None:
                self.connectNicelyUntil = \
                    currentTime + self.reconnectToMissingIn
            if currentTime <= self.connectNicelyUntil:
                names = list(self.registry.keys())
                names.append(self.name)
                nices = set(distributedConnectionMap(names)[self.name])
                for name in nices:
                    logger.debug("{} being nice and waiting for {} to join".
                                 format(self, name))
                missing = missing.difference(nices)

            for name in missing:
                self.connect(name)

    def handleDisconnectedRemote(self, cur, disconn):
        """

        :param disconn: disconnected remote
        """

        # if disconn.main:
        #     logger.trace("{} remote {} is main, so skipping".
        #                  format(self, disconn.uid))
        #     return

        logger.trace("{} handling disconnected remote {}".format(self, disconn))

        if disconn.joinInProcess():
            logger.trace("{} join already in process, so "
                         "waiting to check for reconnects".
                         format(self))
            self.nextCheck = min(self.nextCheck,
                                 cur + self.reconnectToDisconnectedIn)
            return

        if disconn.allowInProcess():
            logger.trace("{} allow already in process, so "
                         "waiting to check for reconnects".
                         format(self))
            self.nextCheck = min(self.nextCheck,
                                 cur + self.reconnectToDisconnectedIn)
            return

        if disconn.name not in self.registry:
            # TODO this is almost identical to line 615; make sure we refactor
            regName = self.findInNodeRegByHA(disconn.ha)
            if regName:
                logger.debug("{} forgiving name mismatch for {} with same "
                             "ha {} using another name {}".
                             format(self, regName, disconn.ha, disconn.name))
            else:
                logger.debug("{} skipping reconnect on {} because "
                             "it's not found in the registry".
                             format(self, disconn.name))
                return
        count, last = self.lastcheck.get(disconn.uid, (0, 0))
        dname = self.getRemoteName(disconn)
        # TODO come back to ratcheting retries
        # secsSinceLastCheck = cur - last
        # secsToWait = self.ratchet.get(count)
        # secsToWaitNext = self.ratchet.get(count + 1)
        # if secsSinceLastCheck > secsToWait:
        # extra = "" if not last else "; needed to wait at least {} and " \
        #                             "waited {} (next try will be {} " \
        #                             "seconds)".format(round(secsToWait, 2),
        #                                         round(secsSinceLastCheck, 2),
        #                                         round(secsToWaitNext, 2)))

        logger.debug("{} retrying to connect with {}".
                     format(self, dname))
        self.lastcheck[disconn.uid] = count + 1, cur
        # self.nextCheck = min(self.nextCheck,
        #                      cur + secsToWaitNext)
        if disconn.joinInProcess():
            logger.debug("waiting, because join is already in "
                         "progress")
        elif disconn.joined:
            self.updateStamp()
            self.allow(uid=disconn.uid, cascade=True, timeout=20)
            logger.debug("{} disconnected node {} is joined".format(
                self, disconn.name), extra={"cli": "STATUS"})
        else:
            self.connect(dname, disconn.uid)

    def findInNodeRegByHA(self, remoteHa):
        """
        Returns the name of the remote by HA if found in the node registry, else
        returns None
        """
        regName = [nm for nm, ha in self.registry.items()
                   if self.sameAddr(ha, remoteHa)]
        if len(regName) > 1:
            raise RuntimeError("more than one node registry entry with the "
                               "same ha {}: {}".format(remoteHa, regName))
        if regName:
            return regName[0]
        return None

    def reconcileNodeReg(self):
        """
        Handle remotes missing from the node registry and clean up old remotes
        no longer in this node's registry.

        1. nice bootstrap
        2. force bootstrap
        3. retry connections

        1. not in remotes
        2.     in remotes, not joined, not allowed, not join in process
        3.     in remotes, not joined, not allowed,     join in process
        4.     in remotes,     joined, not allowed, not allow in process
        5.     in remotes,     joined, not allowed,     allow in process
        6.     in remotes,     joined,     allowed,

        :return: the missing remotes
        """
        matches = set()  # good matches found in nodestack remotes
        legacy = set()  # old remotes that are no longer in registry
        conflicts = set()  # matches found, but the ha conflicts
        logger.debug("{} nodereg is {}".
                     format(self, self.registry.items()))
        logger.debug("{} remotes are {}".
                     format(self, [r.name for r in self.remotes.values()]))

        for r in self.remotes.values():
            if r.name in self.registry:
                if self.sameAddr(r.ha, self.registry[r.name]):
                    matches.add(r.name)
                    logger.debug("{} matched remote is {} {}".
                                 format(self, r.uid, r.ha))
                else:
                    conflicts.add((r.name, r.ha))
                    # error("{} ha for {} doesn't match. ha of remote is {} but "
                    #       "should be {}".
                    #       format(self, r.name, r.ha, self.registry[r.name]))
                    logger.error("{} ha for {} doesn't match. ha of remote is {} but "
                                 "should be {}".
                                 format(self, r.name, r.ha, self.registry[r.name]))
            else:
                regName = self.findInNodeRegByHA(r.ha)

                # This change fixes test
                # `testNodeConnectionAfterKeysharingRestarted` in
                # `test_node_connection`
                # regName = [nm for nm, ha in self.nodeReg.items() if ha ==
                #            r.ha and (r.joined or r.joinInProcess())]
                logger.debug("{} unmatched remote is {} {}".
                             format(self, r.uid, r.ha))
                if regName:
                    logger.debug("{} forgiving name mismatch for {} with same "
                                 "ha {} using another name {}".
                                 format(self, regName, r.ha, r.name))
                    matches.add(regName)
                else:
                    logger.debug("{} found a legacy remote {} "
                                 "without a matching ha {}".
                                 format(self, r.name, r.ha))
                    logger.info(str(self.registry))
                    legacy.add(r)

        # missing from remotes... need to connect
        missing = set(self.registry.keys()) - matches

        if len(missing) + len(matches) + len(conflicts) != len(self.registry):
            logger.error("Error reconciling nodeReg with remotes")
            logger.error("missing: {}".format(missing))
            logger.error("matches: {}".format(matches))
            logger.error("conflicts: {}".format(conflicts))
            logger.error("nodeReg: {}".format(self.registry.keys()))
            logger.error("Error reconciling nodeReg with remotes; see logs")

        if conflicts:
            logger.error("found conflicting address information {} in registry"
                         .format(conflicts))
        if legacy:
            for l in legacy:
                logger.error("{} found legacy entry [{}, {}] in remotes, "
                             "that were not in registry".
                             format(self, l.name, l.ha))
                self.removeRemote(l)
        return missing

    def getRemoteName(self, remote):
        """
        Returns the name of the remote object if found in node registry.

        :param remote: the remote object
        """
        if remote.name not in self.registry:
            find = [name for name, ha in self.registry.items()
                    if ha == remote.ha]
            assert len(find) == 1
            return find[0]
        return remote.name

