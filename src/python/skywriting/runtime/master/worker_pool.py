# Copyright (c) 2010 Derek Murray <derek.murray@cl.cam.ac.uk>
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

'''
Created on Apr 15, 2010

@author: derek
'''
from __future__ import with_statement
from cherrypy.process import plugins
from Queue import Queue
from threading import Lock, Condition
from skywriting.runtime.block_store import SWReferenceJSONEncoder
from skywriting.runtime.master.task_pool import TASK_ASSIGNED
import random
import datetime
import sys
import simplejson
import httplib2

class FeatureQueues:
    def __init__(self):
        self.queues = {}
        
    def get_queue_for_feature(self, feature):
        try:
            return self.queues[feature]
        except KeyError:
            queue = Queue()
            self.queues[feature] = queue
            return queue

class Worker:
    
    def __init__(self, worker_id, worker_descriptor, feature_queues):
        self.id = worker_id
        self.netloc = worker_descriptor['netloc']
        self.features = worker_descriptor['features']
        self.current_task_id = None
        self.last_ping = datetime.datetime.now()
        
        self.failed = False
        
        self.local_queue = Queue()
        self.queues = [self.local_queue]
        for feature in self.features:
            self.queues.append(feature_queues.get_queue_for_feature(feature))

    def __repr__(self):
        return 'Worker(%d)' % self.id

    def as_descriptor(self):
        return {'worker_id': self.id,
                'netloc': self.netloc,
                'features': self.features,
                'current_task_id': str(self.current_task_id),
                'last_ping': self.last_ping.ctime(),
                'failed':  self.failed}
        

class WorkerPool(plugins.SimplePlugin):
    
    def __init__(self, bus, deferred_worker):
        plugins.SimplePlugin.__init__(self, bus)
        self.deferred_worker = deferred_worker
        self.idle_worker_queue = Queue()
        self.current_worker_id = 0
        self.workers = {}
        self.netlocs = {}
        self.idle_set = set()
        self._lock = Lock()
        self.feature_queues = FeatureQueues()
        self.event_count = 0
        self.event_condvar = Condition(self._lock)
        self.max_concurrent_waiters = 5
        self.current_waiters = 0
        self.is_stopping = False
        
    def subscribe(self):
        self.bus.subscribe('worker_failed', self.worker_failed)
        self.bus.subscribe('worker_idle', self.worker_idle)
        self.bus.subscribe('worker_ping', self.worker_ping)
        self.bus.subscribe('stop', self.server_stopping, 10) 
        self.deferred_worker.do_deferred_after(30.0, self.reap_dead_workers)
        
    def unsubscribe(self):
        self.bus.unsubscribe('worker_failed', self.worker_failed)
        self.bus.unsubscribe('worker_idle', self.worker_idle)
        self.bus.unsubscribe('worker_ping', self.worker_ping)
        self.bus.unsubscribe('stop', self.server_stopping) 
        
    def create_worker(self, worker_descriptor):
        with self._lock:
            id = self.current_worker_id
            self.current_worker_id += 1
            worker = Worker(id, worker_descriptor, self.feature_queues)
            self.workers[id] = worker
            self.netlocs[worker.netloc] = worker
            self.idle_set.add(id)
            self.event_count += 1
            self.event_condvar.notify_all()
            
        self.bus.publish('schedule')
        return id
    
    def shutdown(self):
        for worker in self.workers.values():
            try:
                httplib2.Http().request('http://%s/kill/' % worker.netloc)
            except:
                pass
        
    def get_worker_by_id(self, id):
        with self._lock:
            return self.workers[id]
        
    def get_idle_workers(self):
        with self._lock:
            return map(lambda x: self.workers[x], self.idle_set)
    
    def execute_task_on_worker_id(self, worker_id, task):
        with self._lock:
            self.idle_set.remove(worker_id)
            worker = self.workers[worker_id]
            worker.current_task_id = task.task_id
            task.set_assigned_to_worker(worker_id)
            task.state = TASK_ASSIGNED
            task.record_event("ASSIGNED")
            task.worker_id = worker_id
            self.event_count += 1
            self.event_condvar.notify_all()
            
        try:
            httplib2.Http().request("http://%s/task/" % (worker.netloc), "POST", simplejson.dumps(task.as_descriptor(), cls=SWReferenceJSONEncoder), )
        except:
            print sys.exc_info()
            print 'Worker failed:', worker_id
            self.worker_failed(worker_id)
            
    def abort_task_on_worker(self, task):
        worker_id = task.worker_id
        with self._lock:
            worker = self.workers[worker_id]
    
        try:
            print "Aborting task %d on worker %d" % (task.task_id, worker_id)
            response, _ = httplib2.Http().request('http://%s/task/%d/abort' % (worker.netloc, task.task_id), 'POST')
            if response.status == 200:
                self.worker_idle(worker_id)
            else:
                print response
                print 'Worker failed to abort a task:', worker_id 
                self.worker_failed(worker_id)
        except:
            print sys.exc_info()
            print 'Worker failed:', worker_id
            self.worker_failed(worker_id)
    
    def worker_failed(self, id):
        with self._lock:
            self.event_count += 1
            self.event_condvar.notify_all()
            self.idle_set.discard(id)
            failed_task = self.workers[id].current_task_id
            del self.netlocs[self.workers[id].netloc]
            self.workers[id].failed = True

        if failed_task is not None:
            self.bus.publish('task_failed', failed_task, 'WORKER_FAILED')
        
    def worker_idle(self, id):
        with self._lock:
            self.workers[id].current_task_id = None
            self.idle_set.add(id)
            self.event_count += 1
            self.event_condvar.notify_all()
        self.bus.publish('schedule')
            
    def worker_ping(self, id, status, ping_news):
        with self._lock:
            worker = self.workers[id]
            self.event_count += 1
            self.event_condvar.notify_all()
        worker.last_ping = datetime.datetime.now()
        
    def get_all_workers(self):
        with self._lock:
            return 

    def get_all_workers_with_version(self):
        with self._lock:
            return (self.event_count, map(lambda x: x.as_descriptor(), self.workers.values()))

    def server_stopping(self):
        with self._lock:
            self.is_stopping = True
            self.event_condvar.notify_all()

    def await_version_after(self, target):
        with self._lock:
            self.current_waiters = self.current_waiters + 1
            while self.event_count <= target:
                if self.current_waiters > self.max_concurrent_waiters:
                    break
                elif self.is_stopping:
                    break
                else:
                    self.event_condvar.wait()
            self.current_waiters = self.current_waiters - 1
            if self.current_waiters >= self.max_concurrent_waiters:
                raise Exception("Too many concurrent waiters")
            elif self.is_stopping:
                raise Exception("Server stopping")
            return self.event_count

    def investigate_worker_failure(self, worker):
        print 'In investigate_worker_failure for', worker
        try:
            response, _ = httplib2.Http().request('http://%s/' % (worker.netloc, ), 'GET')
        except:
            print 'Worker', worker, 'has failed'
            self.bus.publish('worker_failed', worker.id)

    def get_random_worker(self):
        with self._lock:
            return random.choice(self.workers.values())
        
    def get_worker_at_netloc(self, netloc):
        try:
            return self.netlocs[netloc]
        except KeyError:
            return None

    def reap_dead_workers(self):
        if not self.is_stopping:
            for worker in self.workers.values():
                if worker.failed:
                    continue
                if (worker.last_ping + datetime.timedelta(seconds=30)) < datetime.datetime.now():
                    failed_worker = worker
                    self.deferred_worker.do_deferred(lambda: self.investigate_worker_failure(failed_worker))
                    
            self.deferred_worker.do_deferred_after(30.0, self.reap_dead_workers)