from collections import OrderedDict
import ujson as json
import multiprocessing
from multiprocessing import Process
import os
import random
import time
import traceback
import zmq
import zmq.decorators as zmqd
from aser.conceptualize.aser_conceptualizer import SeedRuleASERConceptualizer, ProbaseASERConceptualizer
from aser.eventuality import Eventuality
from aser.relation import Relation
from aser.concept import ASERConcept
from aser.database.kg_connection import ASERKGConnection, ASERConceptConnection
from aser.server.utils import *
from aser.extract.aser_extractor import SeedRuleASERExtractor, DiscourseASERExtractor
from aser.utils.config import ASERCmd, ASERError

CACHESIZE = 512

class ASERServer(object):
    """ ASER server to provide extraction, conceptualization, and retrieval functions

    """
    def __init__(self, opt):
        """

        :param opt: the namespace that includes parameters
        :type opt: argparse.Namespace
        """
        self.opt = opt
        self.port = opt.port
        self.n_concurrent_back_socks = opt.n_concurrent_back_socks
        self.n_workers = opt.n_workers
        self.aser_sink = None
        self.aser_db = None
        self.aser_workers = []

        self.run()

    def run(self):
        """ Start the server

        """
        self._run()

    def close(self):
        """ Clase the server safely

        """
        self.aser_sink.close()
        self.aser_db.close()
        for worker in self.aser_workers:
            worker.close()
        for corenlp in self.corenlp_servers:
            corenlp.close()

    @zmqd.context()
    @zmqd.socket(zmq.PULL)
    @zmqd.socket(zmq.PAIR)
    @zmqd.socket(zmq.PUSH)
    def _run(self, ctx, client_msg_receiver, sink_addr_receiver, db_sender):
        total_st = time.time()

        client_msg_receiver.bind("tcp://*:%d" % self.port)

        sink_addr_receiver_addr = sockets_ipc_bind(sink_addr_receiver)
        self.aser_sink = ASERSink(self.opt, sink_addr_receiver_addr)
        self.aser_sink.start()
        sink_receiver_addr = sink_addr_receiver.recv().decode("utf-8")

        db_senders = []
        db_addr_list = []
        for _ in range(self.n_concurrent_back_socks):
            _socket = ctx.socket(zmq.PUSH)
            addr = sockets_ipc_bind(_socket)
            db_senders.append(_socket)
            db_addr_list.append(addr)

        self.aser_db = ASERDataBase(self.opt, db_addr_list, sink_receiver_addr)
        self.aser_db.start()

        worker_senders = []
        worker_addr_list = []
        for _ in range(self.n_concurrent_back_socks):
            _socket = ctx.socket(zmq.PUSH)
            addr = sockets_ipc_bind(_socket)
            worker_senders.append(_socket)
            worker_addr_list.append(addr)

        for i in range(self.n_workers):
            self.aser_workers.append(
                ASERWorker(self.opt, i, worker_addr_list, sink_receiver_addr)
            )
            self.aser_workers[i].start()

        print("Loading Server Finished in {:.4f} s".format(time.time() - total_st))
        worker_sender_id = -1
        db_sender_id = -1
        cnt = 0
        st = time.time()
        while True:
            try:
                client_msg = client_msg_receiver.recv_multipart()
                client_id, req_id, cmd, data = client_msg
                if cmd in [
                    ASERCmd.parse_text, ASERCmd.extract_eventualities, ASERCmd.extract_relations,
                    ASERCmd.extract_eventualities_and_relations, ASERCmd.conceptualize_eventuality,
                ]:
                    worker_sender_id, worker_sender = random.choice(
                        [(i, sender) for i, sender in enumerate(worker_senders)
                         if i != worker_sender_id])
                    worker_sender.send_multipart(client_msg)
                else:
                    db_sender_id, db_sender = random.choice(
                        [(i, sender) for i, sender in enumerate(db_senders)
                         if i != db_sender_id])
                    db_sender.send_multipart(client_msg)
                cnt += 1
                # print("sender speed: {:.4f} / call".format((time.time() - st) / cnt))
                print("Sender cnt {}".format(cnt))
            except Exception:
                print(traceback.format_exc())


class ASERDataBase(Process):
    """ Process to provide DB retrieval functions

    """
    def __init__(self, opt, db_sender_addr_list, sink_addr):
        super().__init__()
        self.db_sender_addr_list = db_sender_addr_list
        self.sink_addr = sink_addr

        if opt.aser_kg_dir:
            print("Connect to the ASER KG...")
            st = time.time()
            self.kg_conn = ASERKGConnection(db_path=os.path.join(opt.aser_kg_dir, "KG.db"), mode="cache")
            print("Connect to the ASER KG finished in {:.4f} s".format(time.time() - st))
        else:
            print("Skip loading the ASER KG")
            self.kg_conn = None

        if opt.concept_kg_dir:
            print("Connect to the ASER Concept KG...")
            st = time.time()
            self.concept_conn = ASERConceptConnection(db_path=os.path.join(opt.concept_kg_dir, "concept.db"), mode="cache")
            print("Connect to the ASER Concept KG finished in {:.4f} s".format(time.time() - st))
        else:
            print("Skip loading the ASER Concept KG")
            self.concept_conn = None

    def run(self):
        self._run()

    def close(self):
        if self.kg_conn:
            self.kg_conn.close()
        if self.concept_conn:
            self.concept_conn.close()
        self.terminate()
        self.join()

    @zmqd.context()
    @zmqd.socket(zmq.PUSH)
    def _run(self, ctx, sink):
        receiver_sockets = []
        poller = zmq.Poller()
        for db_sender_addr in self.db_sender_addr_list:
            _socket = ctx.socket(zmq.PULL)
            _socket.connect(db_sender_addr)
            receiver_sockets.append(_socket)
            poller.register(_socket)
        sink.connect(self.sink_addr)

        cnt = 0
        st = time.time()
        while True:
            try:
                eventualities = dict(poller.poll())
                for sock_idx, sock in enumerate(receiver_sockets):
                    if sock in eventualities:
                        client_id, req_id, cmd, data = sock.recv_multipart()
                        print("DB received msg ({}, {}, {}, {})".format(
                            client_id.decode("utf-8"), req_id.decode("utf-8"),
                            cmd.decode("utf-8"), data.decode("utf-8")
                        ))
                        try:
                            if cmd == ASERCmd.exact_match_eventuality:
                                ret_data = self.handle_exact_match_eventuality(data)
                            elif cmd == ASERCmd.exact_match_eventuality_relation:
                                ret_data = self.handle_exact_match_eventuality_relation(data)
                            elif cmd == ASERCmd.fetch_related_eventualities:
                                ret_data = self.handle_fetch_related_eventualities(data)
                            elif cmd == ASERCmd.exact_match_concept:
                                ret_data = self.handle_exact_match_concept(data)
                            elif cmd == ASERCmd.exact_match_concept_relation:
                                ret_data = self.handle_exact_match_concept_relation(data)
                            elif cmd == ASERCmd.fetch_related_concepts:
                                ret_data = self.handle_fetch_related_concepts(data)
                            else:
                                raise ValueError("Error: %s cmd is invalid" % (cmd))
                        except BaseException as e:
                            print(e)
                            ret_data = json.dumps(ASERError + e.__repr__()).encode("utf-8")
                        sink.send_multipart([client_id, req_id, cmd, ret_data])
                        cnt += 1
                        print("DB cnt {}".format(cnt))
                # print("DB speed: {:.4f} / call".format((time.time() - st) / cnt))
            except Exception:
                print(traceback.format_exc())

    def handle_exact_match_eventuality(self, data):
        data = data.decode("utf-8")
        if isinstance(data, str): # eid
            matched_eventuality = self.kg_conn.get_exact_match_eventuality(data)
        else:
            data = Eventuality().decode(json.loads(data), encoding=None)
            matched_eventuality = self.kg_conn.get_exact_match_eventuality(data)

        if matched_eventuality:
            ret_data = json.dumps(matched_eventuality.encode(encoding=None)).encode("utf-8")
        else:
            ret_data = json.dumps(ASERCmd.none).encode(encoding="utf-8")
        return ret_data

    def handle_exact_match_eventuality_relation(self, data):
        data = data.decode("utf-8")
        if isinstance(data, str): # rid
            matched_relation = self.kg_conn.get_exact_match_relation(data)
        else:
            data = Relation().decode(json.loads(data), encoding=None)
            matched_relation = self.kg_conn.get_exact_match_relation(data)

        if matched_relation:
            ret_data = json.dumps(matched_relation.encode(encoding=None)).encode("utf-8")
        else:
            ret_data = json.dumps(ASERCmd.none).encode(encoding="utf-8")
        return ret_data

    def handle_fetch_related_eventualities(self, data):
        data = data.decode("utf-8")
        if isinstance(data, str): # hid
            related_eventualities = self.kg_conn.get_related_eventualities(data)
        else:
            data = Eventuality().decode(json.loads(data), encoding=None)
        rst = [(eventuality.encode(encoding=None), relation.encode(encoding=None))
               for eventuality, relation in related_eventualities]
        ret_data = json.dumps(rst).encode("utf-8")
        return ret_data

    def handle_exact_match_concept(self, data):
        data = data.decode("utf-8")
        if isinstance(data, str): # eid
            matched_concept = self.concept_conn.get_exact_match_concept(data)
        else:
            data = ASERConcept().decode(json.loads(data), encoding=None)
            matched_concept = self.concept_conn.get_exact_match_concept(data)

        if matched_concept:
            ret_data = json.dumps(matched_concept.encode(encoding=None)).encode("utf-8")
        else:
            ret_data = json.dumps(ASERCmd.none).encode(encoding="utf-8")
        return ret_data

    def handle_exact_match_concept_relation(self, data):
        data = data.decode("utf-8")
        if isinstance(data, str): # rid
            matched_relation = self.concept_conn.get_exact_match_relation(data)
        else:
            data = Relation().decode(json.loads(data), encoding=None)
            matched_relation = self.concept_conn.get_exact_match_relation(data)

        if matched_relation:
            ret_data = json.dumps(matched_relation.encode(encoding=None)).encode("utf-8")
        else:
            ret_data = json.dumps(ASERCmd.none).encode(encoding="utf-8")
        return ret_data

    def handle_fetch_related_concepts(self, data):
        data = data.decode("utf-8")
        if isinstance(data, str): # hid
            related_concepts = self.concept_conn.get_related_concepts(data)
        else:
            data = ASERConcept().decode(json.loads(data), encoding=None)
        rst = [(concept.encode(encoding=None), relation.encode(encoding=None))
               for concept, relation in related_concepts]
        ret_data = json.dumps(rst).encode("utf-8")
        return ret_data


class ASERWorker(Process):
    """ Process to serve extraction and conceptualization functions

    """
    def __init__(self, opt, id, worker_addr_list, sink_addr):
        super().__init__()
        self.worker_id = id
        self.worker_addr_list = worker_addr_list
        self.sink_addr = sink_addr
        self.aser_extractor = DiscourseASERExtractor(
            corenlp_path=opt.corenlp_path,
            corenlp_port=opt.base_corenlp_port + id
        )
        print("Eventuality Extractor init finished")
        if opt.concept_method == "seed":
            self.conceptualizer = SeedRuleASERConceptualizer()
        elif opt.probase_path:
            if opt.probase_path:
                self.conceptualizer = ProbaseASERConceptualizer(probase_path=opt.probase_path, probase_topk=opt.concept_topk)
            else:
                self.conceptualizer = None
        else:
            self.conceptualizer = None
        print("Concept Extractor init finished")
        self.is_ready = multiprocessing.Event()
        self.worker_cache = OrderedDict()

    def run(self):
        self._run()

    def close(self):
        self.is_ready.clear()
        self.aser_extractor.close()
        self.terminate()
        self.join()

    @zmqd.context()
    @zmqd.socket(zmq.PUSH)
    def _run(self, ctx, sink):
        print("ASER Worker %d started" % self.worker_id)
        receiver_sockets = []
        poller = zmq.Poller()
        for worker_addr in self.worker_addr_list:
            _socket = ctx.socket(zmq.PULL)
            _socket.connect(worker_addr)
            receiver_sockets.append(_socket)
            poller.register(_socket)
        sink.connect(self.sink_addr)

        while True:
            try:
                eventualities = dict(poller.poll())
                for sock_idx, sock in enumerate(receiver_sockets):
                    if sock in eventualities:
                        client_id, req_id, cmd, data = sock.recv_multipart()
                        print("Worker {} received msg ({}, {}, {}, {})".format(
                            self.worker_id,
                            client_id.decode("utf-8"), req_id.decode("utf-8"),
                            cmd.decode("utf-8"), data.decode("utf-8")
                        ))
                        try:
                            if cmd == ASERCmd.parse_text:
                                ret_data = self.handle_parse_text(data)
                            elif cmd == ASERCmd.extract_eventualities:
                                ret_data = self.handle_extract_eventualities(data)
                            elif cmd == ASERCmd.extract_relations:
                                ret_data = self.handle_extract_relations(data)
                            elif cmd == ASERCmd.extract_eventualities_and_relations:
                                ret_data = self.extract_eventualities_and_relations(data)
                            elif cmd == ASERCmd.conceptualize_eventuality:
                                ret_data = self.handle_conceptualize_eventuality(data)
                            else:
                                raise ValueError("Error: %s cmd is invalid" % (cmd))
                        except BaseException as e:
                            print(e)
                            ret_data = json.dumps(ASERError + e.__repr__()).encode("utf-8")
                        sink.send_multipart([client_id, req_id, cmd, ret_data])
            except Exception:
                print(traceback.format_exc())

    def handle_parse_text(self, data):
        data = data.decode("utf-8")
        key = (ASERCmd.parse_text, data)
        if key in self.worker_cache:
            return self.worker_cache[key]

        parser_results = self.aser_extractor.parse_text(data)
        ret_data = json.dumps(parser_results).encode("utf-8")
        if len(self.worker_cache) >= CACHESIZE:
            self.worker_cache.popitem(last=False)
        self.worker_cache[key] = ret_data
        return ret_data

    def handle_extract_eventualities(self, data):
        data = data.decode("utf-8")

        if isinstance(data, str): # text
            key = (ASERCmd.extract_eventualities, data)
            if key in self.worker_cache:
                return self.worker_cache[key]
            para_eventualities = self.aser_extractor.extract_eventualities_from_text(data)
        else: # parsed results
            key = (ASERCmd.extract_eventualities, " ".join([sent_parsed_result["text"] for sent_parsed_result in data]))
            if key in self.worker_cache:
                return self.worker_cache[key]
            para_eventualities = self.aser_extractor.extract_eventualities_from_parsed_result(data)
        para_eventualities = [[e.encode(encoding=None) for e in sent_eventualities] for sent_eventualities in para_eventualities]
        ret_data = json.dumps(para_eventualities).encode("utf-8")
        if len(self.worker_cache) >= CACHESIZE:
            self.worker_cache.popitem(last=False)
        self.worker_cache[key] = ret_data
        return ret_data

    def handle_extract_relations(self, data):
        data = data.decode("utf-8")

        if isinstance(data, str): # text
            key = (ASERCmd.extract_relations, data)
            if key in self.worker_cache:
                return self.worker_cache[key]
            para_relations = self.aser_extractor.extract_relations_from_text(data)
        else:
            data = json.loads(data)
            if len(data) == 2:
                parsed_results = data[0]
                para_eventualities = [[Eventuality().decode(e_encoded, encoding=None) for e_encoded in sent_eventualities] for sent_eventualities in data[1]]
                key = (
                    ASERCmd.extract_relations,
                    " ".join([sent_parsed_result["text"] for sent_parsed_result in parsed_results]),
                    str([[e.eid for e in sent_eventualities] for sent_eventualities in para_eventualities])
                )
                if key in self.worker_cache:
                    return self.worker_cache[key]
                para_relations = self.aser_extractor.extract_relations_from_parsed_result(parsed_results, para_eventualities)
            else:
                raise ValueError("Error: your message should be text or (parsed_results, para_eventualities).")
        para_relations = [[r.encode(encoding=None) for r in sent_relations] for sent_relations in para_relations]
        ret_data = json.dumps(para_relations).encode("utf-8")
        if len(self.worker_cache) >= CACHESIZE:
            self.worker_cache.popitem(last=False)
        self.worker_cache[key] = ret_data
        return ret_data

    def handle_extract_eventualities_and_relations(self, data):
        data = data.decode("utf-8")

        if isinstance(data, str): # text
            key = (ASERCmd.extract_eventualities_and_relations, data)
            if key in self.worker_cache:
                return self.worker_cache[key]
            para_eventualities, para_relations = self.aser_extractor.extract_from_text(data)
        else: # parsed results
            key = (ASERCmd.extract_eventualities_and_relations, " ".join([sent_parsed_result["text"] for sent_parsed_result in data]))
            if key in self.worker_cache:
                return self.worker_cache[key]
            para_eventualities, para_relations = self.aser_extractor.extract_from_parsed_result(data)
        para_eventualities = [[e.encode(encoding=None) for e in sent_eventualities] for sent_eventualities in para_eventualities]
        para_relations = [[r.encode(encoding=None) for r in sent_relations] for sent_relations in para_relations]
        ret_data = json.dumps((para_eventualities, para_relations)).encode("utf-8")
        if len(self.worker_cache) >= CACHESIZE:
            self.worker_cache.popitem(last=False)
        self.worker_cache[key] = ret_data
        return ret_data

    def handle_conceptualize_eventuality(self, data):
        eventuality = Eventuality().decode(data, encoding="utf-8")
        key = (ASERCmd.conceptualize_eventuality, eventuality.eid)
        if key in self.worker_cache:
            return self.worker_cache[key]

        concepts = self.conceptualizer.conceptualize(eventuality)
        concepts = [(concept.encode(encoding=None), score) for concept, score in concepts]
        ret_data = json.dumps(concepts).encode("utf-8")
        if len(self.worker_cache) >= CACHESIZE:
            self.worker_cache.popitem(last=False)
        self.worker_cache[key] = ret_data
        return ret_data
        # rst = []
        # ret_list = list()
        # for concept, score in concepts:
        #     ret_list.append((concept.words, score))
        # ret_data = json.dumps(ret_list).encode("utf-8")
        # return ret_data


class ASERSink(Process):
    """ Process to forward messages

    """
    def __init__(self, args, sink_addr_receiver_addr):
        super().__init__()
        self.port_out = args.port_out
        self.sink_addr_receiver_addr = sink_addr_receiver_addr

    def run(self):
        self._run()

    @zmqd.context()
    @zmqd.socket(zmq.PAIR)
    @zmqd.socket(zmq.PULL)
    @zmqd.socket(zmq.PUB)
    def _run(self, _, addr_sender, receiver, sender):
        addr_sender.connect(self.sink_addr_receiver_addr)
        receiver_addr = sockets_ipc_bind(receiver).encode("utf-8")
        addr_sender.send(receiver_addr)
        sender.bind("tcp://*:%d" % self.port_out)
        print("ASER Sink started")
        cnt = 0
        while True:
            try:
                msg = receiver.recv_multipart()
                sender.send_multipart(msg)
                cnt += 1
                print("Sink cnt {}".format(cnt))
            except Exception:
                print(traceback.format_exc())
