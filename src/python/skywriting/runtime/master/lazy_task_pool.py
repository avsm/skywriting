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

from skywriting.runtime.task import TASK_CREATED, TASK_BLOCKING, TASK_RUNNABLE,\
    TASK_ASSIGNED, TASK_COMMITTED, build_taskpool_task_from_descriptor,\
    TASK_QUEUED, TASK_FAILED
import collections
from skywriting.runtime.references import SW2_FutureReference,\
    SW2_ConcreteReference, SWErrorReference
import uuid
from threading import Lock
from Queue import Queue
from skywriting.runtime.master.job_pool import Job
import cherrypy
from cherrypy.process import plugins
import logging

class LazyTaskPool(plugins.SimplePlugin):
    
    def __init__(self, bus):
    
        # Used for publishing schedule events.
        self.bus = bus
    
        # Mapping from task ID to task object.
        self.tasks = {}
        
        # Mapping from expected output to producing task.
        self.task_for_output = {}
        
        # Mapping from expected output to consuming tasks.
        self.consumers_for_output = {}
        
        # Mapping from output name to concrete reference.
        self.ref_for_output = {}
        
        # Current set of job outputs: i.e. expected outputs that we want to
        # produce by lazy graph reduction.
        self.job_outputs = {}
        
        # A thread-safe queue of runnable tasks, which we use to pass tasks to
        # the LazyScheduler.
        self.task_queue = Queue()
        
        # At the moment, this is a coarse-grained lock, which is acquired when
        # a task is added or completed, or when references are externally
        # published.
        self._lock = Lock()
        
    def subscribe(self):
        self.bus.subscribe('task_failed', self.task_failed)
        
    def unsubscribe(self):
        self.bus.unsubscribe('task_failed', self.task_failed)
        
    def get_task_by_id(self, task_id):
        return self.tasks[task_id]
        
    def add_task(self, task, is_root_task=False):
        # XXX: This will no longer be true when we move to deterministic task
        # names.
        assert task.task_id not in self.tasks
        
        self.tasks[task.task_id] = task
        if is_root_task:
            self.job_outputs[task.expected_outputs[0]] = task.job
            self.register_job_interest_for_output(task.expected_outputs[0], task.job)
        else:
            # The task inherits the parent task's job.
            # XXX: We should probably do this outside the lazy task pool.
            task.job.add_task(task)
        
        # If any of the task outputs are being waited on, we should reduce this
        # task's graph. 
        with self._lock:
            should_reduce = self.register_task_outputs(task)
            if should_reduce:
                self.do_graph_reduction(root_tasks=[task])
            elif is_root_task:
                self.do_root_graph_reduction()
            
    def task_completed(self, task, commit_bindings):
        task.set_state(TASK_COMMITTED)
        worker = task.worker
        
        # Need to notify all of the consumers, which may make other tasks
        # runnable.
        self.publish_refs(commit_bindings)
        self.bus.publish('worker_idle', worker)
        
    def get_task_queue(self):
        return self.task_queue
        
    def task_failed(self, task_id, reason, details=None):

        cherrypy.log.error('Task failed because %s' % (reason, ), 'TASKPOOL', logging.WARNING)
        worker = None
        should_notify_outputs = False
        task = self.tasks[task_id]

        with self._lock:
            if reason == 'WORKER_FAILED':
                # Try to reschedule task.
                task.current_attempt += 1
                # XXX: Remove this hard-coded constant. We limit the number of
                #      retries in case the task is *causing* the failures.
                if task.current_attempt > 3:
                    task.set_state(TASK_FAILED)
                    should_notify_outputs = True
                else:
                    cherrypy.log.error('Rescheduling task %s after worker failure' % task_id, 'TASKPOOL', logging.WARNING)
                    task.set_state(TASK_FAILED)
                    self.add_runnable_task(task)
                    self.bus.publish('schedule')
                    
            elif reason == 'MISSING_INPUT':
                # Problem fetching input, so we will have to re-execute it.
                worker = task.worker
                self.handle_missing_input(task, details)
                
            elif reason == 'RUNTIME_EXCEPTION':
                # A hard error, so kill the entire job, citing the problem.
                self.handle_runtime_exception(task)
                worker = task.worker
                task.set_state(TASK_FAILED)
                should_notify_outputs = True

        # Doing this outside the lock because this leads via add_refs_to_id
        # --> self::reference_available, creating a circular wait. We noted the task as FAILED inside the lock,
        # which ought to be enough.
        if should_notify_outputs:
            for output in task.expected_outputs:
                self._publish_ref(output, SWErrorReference(reason, details))

        if worker is not None:
            self.bus.publish('worker_idle', worker)
    
    def handle_missing_input(self, task, input_ref):
        task.set_state(TASK_FAILED)
        
        # We will re-reduce the graph for this task, ignoring the network
        # locations for which getting the input failed.
        assert isinstance(input_ref, SW2_ConcreteReference)
        ignore_netlocs = input_ref.location_hints.keys()
        
        self.do_graph_reduction(root_tasks=[task], ignore_netlocs=ignore_netlocs)
    
    def publish_refs(self, refs):
        with self._lock:
            for global_id, reflist in refs.items():
                # XXX: Currently, we publish a list of refs for each name, 
                #      whereas we should move to publishing a single concrete
                #      ref with many location hints.
                self._publish_ref(global_id, reflist[0])
        
    def _publish_ref(self, global_id, ref):
        
        # Record the name-to-concrete-reference mapping for this ref's name.
        try:
            existing_ref = self.ref_for_output[global_id]
            if isinstance(existing_ref, SW2_ConcreteReference): 
                existing_ref.combine_with(ref)
        except KeyError:
            self.ref_for_output[global_id] = ref
            existing_ref = ref

        # Notify any consumers that the ref is now available. N.B. After this,
        # the consumers are unsubscribed from this ref.
        try:
            consumers = self.consumers_for_output.pop(global_id)
            for consumer in consumers:
                if isinstance(consumer, Job):
                    consumer.completed(existing_ref)
                else:
                    self.notify_task_of_reference(consumer, global_id, existing_ref)
        except KeyError:
            pass

    def notify_task_of_reference(self, task, id, ref):
        task.unblock_on(id, [ref])
        if not task.is_blocked():
            self.add_runnable_task(task)
                
    def register_job_interest_for_output(self, ref_id, job):
        try:
            subscribers = self.consumers_for_output[ref_id]
        except:
            subscribers = set()
            self.consumers_for_output[ref_id] = subscribers
        subscribers.add(job)
            
    def register_task_interest_for_ref(self, task, ref, ignore_netlocs=[]):
        if isinstance(ref, SW2_FutureReference):
            # First, see if we already have a concrete reference for this
            # output.
            try:
                conc_ref = self.ref_for_output[ref.id]
                return conc_ref
            except KeyError:
                pass
            
            # Otherwise, subscribe to the production of the named output.
            try:
                subscribers = self.consumers_for_output[ref.id]
            except:
                subscribers = set()
                self.consumers_for_output[ref.id] = subscribers
            subscribers.add(task)
            return None

        elif isinstance(ref, SW2_ConcreteReference):

            if len(ignore_netlocs) > 0:
                # Exceptional case: we want to ignore the network locations
                # corresponding to one or more failed workers.
                
                # In the mean time, the concrete reference may have been 
                # updated with more locations, so compute the union of the
                # location hints.
                try:
                    conc_ref = self.ref_for_output[ref.id]
                    
                    # We delete the reference, because we will either add back
                    # a modified version, or the reference will be unavailable.
                    del self.ref_for_output[ref.id]
                    
                    ref.combine_with(conc_ref)
                except KeyError:
                    pass
            
                # Delete the failed worker(s) from the combined reference.    
                for netloc in ignore_netlocs:
                    if ref.location_hints.has_key(netloc):
                        del ref.location_hints[netloc]
            
                if len(ref.location_hints) > 0:
                    # In this case, we got lucky because another task has
                    # produced the output that we were seeking.
                    # Store the updated reference minus the ignored netlocs.
                    self.ref_for_output[ref.id] = ref
                    return ref
                else:
                    # The reference is unavailable, so we will need to
                    # reproduce its data.
                    return self.register_task_interest_for_ref(task, 
                                                               ref.as_future(),
                                                               ignore_netlocs)
                
            # We have a concrete reference for this name, but others may
            # be waiting on it, so publish it.
            self._publish_ref(ref.id, ref)
            return ref
        
        else:
            # We have an opaque reference, which can be accessed immediately.
            return ref
        
    def register_task_outputs(self, task):
        # If any tasks have previously registered an interest in any of this
        # task's outputs, we need to reduce the given task.
        should_reduce = False
        for output in task.expected_outputs:
            self.task_for_output[output] = task
            if self.output_has_consumers(output):
                should_reduce = True
        return should_reduce
    
    def output_has_consumers(self, output):
        try:
            subscribers = self.consumers_for_output[output]
            return len(subscribers) > 0
        except KeyError:
            return False
    
    def add_runnable_task(self, task):
        task.set_state(TASK_QUEUED)
        self.task_queue.put(task)
    
    def do_root_graph_reduction(self):
        self.do_graph_reduction(object_ids=self.job_outputs.keys())
    
    def do_graph_reduction(self, object_ids=[], root_tasks=[], ignore_netlocs=[]):
        
        should_schedule = False
        newly_active_task_queue = collections.deque()
        
        # Initially, start with the root set of tasks, based on the desired
        # object IDs.
        for object_id in object_ids:
            task = self.task_for_output[object_id]
            if task.state == TASK_CREATED:
                # Task has not yet been scheduled, so add it to the queue.
                task.set_state(TASK_BLOCKING)
                newly_active_task_queue.append(task)
            
        for task in root_tasks:
            newly_active_task_queue.append(task)
                
        # Do breadth-first search through the task graph to identify other 
        # tasks to make active. We use task.state == TASK_BLOCKING as a marker
        # to prevent visiting a task more than once.
        while len(newly_active_task_queue) > 0:
            
            task = newly_active_task_queue.popleft()
            
            # Identify the other tasks that need to run to make this task
            # runnable.
            task_will_block = False
            for local_id, ref in task.dependencies.items():
                conc_ref = self.register_task_interest_for_ref(task, 
                                                               ref,
                                                               ignore_netlocs=ignore_netlocs)
                if conc_ref is not None:
                    task.inputs[local_id] = conc_ref
                else:
                    
                    # The reference is a future that has not yet been produced,
                    # so block the task.
                    task_will_block = True
                    task.block_on(ref.id, local_id)
                    
                    # We may need to recursively check the inputs on the
                    # producing task for this reference.
                    try:
                        producing_task = self.task_for_output[ref.id]
                    except KeyError:
                        print ref
                        producing_task = self.tasks[ref.provenance.task_id]
                    
                    assert producing_task.state in (TASK_CREATED, 
                                                    TASK_BLOCKING,
                                                    TASK_RUNNABLE, 
                                                    TASK_QUEUED,
                                                    TASK_ASSIGNED,
                                                    TASK_COMMITTED)

                    # The producing task is inactive, so recursively visit it.                    
                    if producing_task.state in (TASK_CREATED, TASK_COMMITTED):
                        producing_task.set_state(TASK_BLOCKING)
                        newly_active_task_queue.append(producing_task)
            
            # If all inputs are available, we can now run this task. Otherwise,
            # it will run when its inputs are published.
            if not task_will_block:
                task.set_state(TASK_RUNNABLE)
                should_schedule = True
                self.add_runnable_task(task)
                
        if should_schedule:
            self.bus.publish('schedule')
    
class LazyTaskPoolAdapter:
    """
    We use this adapter class to convert from the view's idea of a task pool to
    the new LazyTaskPool.
    """
    
    def __init__(self, lazy_task_pool):
        self.lazy_task_pool = lazy_task_pool
     
    def add_task(self, task_descriptor, parent_task_id=None, job=None):
        try:
            task_id = task_descriptor['task_id']
        except:
            task_id = self.generate_task_id()
        
        task = build_taskpool_task_from_descriptor(task_id, task_descriptor, self, parent_task_id)
        task.job = job
        
        self.lazy_task_pool.add_task(task, parent_task_id is None)
        
        #add_event = self.new_event(task)
        #add_event["task_descriptor"] = task.as_descriptor(long=True)
        #add_event["action"] = "CREATED"
    
        #self.events.append(add_event)

        return task
    
    def generate_task_id(self):
        return str(uuid.uuid1())
    
    def get_task_by_id(self, id):
        return self.lazy_task_pool.get_task_by_id(id)
    
    def spawn_child_tasks(self, parent_task, spawned_task_descriptors):

        if parent_task.is_replay_task():
            return
            
        for child in spawned_task_descriptors:
            try:
                spawned_task_id = child['task_id']
            except KeyError:
                raise
            
            task = self.add_task(child, parent_task.task_id, parent_task.job)
            parent_task.children.append(task.task_id)
            
            if task.continues_task is not None:
                parent_task.continuation = spawned_task_id

    def commit_task(self, task_id, commit_payload):
        
        commit_bindings = commit_payload['bindings']
        task = self.lazy_task_pool.get_task_by_id(task_id)
        
        self.lazy_task_pool.task_completed(task, commit_bindings)
        
        # Saved continuation URI, if necessary.
        try:
            commit_continuation_uri = commit_payload['saved_continuation_uri']
            task.saved_continuation_uri = commit_continuation_uri
        except KeyError:
            pass                