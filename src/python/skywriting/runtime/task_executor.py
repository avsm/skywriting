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
Created on 13 Apr 2010

@author: dgm36
'''
from __future__ import with_statement
from skywriting.runtime.plugins import AsynchronousExecutePlugin
from skywriting.lang.context import SimpleContext, TaskContext,\
    LambdaFunction
from skywriting.lang.datatypes import all_leaf_values, map_leaf_values
from skywriting.lang.visitors import \
    StatementExecutorVisitor, SWDereferenceWrapper
from skywriting.lang import ast
from skywriting.runtime.exceptions import ReferenceUnavailableException,\
    FeatureUnavailableException, ExecutionInterruption,\
    SelectException, MissingInputException
from threading import Lock
import cherrypy
import logging
import uuid
from skywriting.runtime.references import SWDataValue, SWURLReference,\
    SWRealReference,\
    SWFutureReference,\
    SWErrorReference, SWNullReference, SW2_FutureReference,\
    SWTaskOutputProvenance, SW2_ConcreteReference, ACCESS_SWBS,\
    SWSpawnedTaskProvenance, SWNoProvenance

class TaskExecutorPlugin(AsynchronousExecutePlugin):
    
    def __init__(self, bus, block_store, master_proxy, execution_features, num_threads=1):
        AsynchronousExecutePlugin.__init__(self, bus, num_threads, "execute_task")
        self.block_store = block_store
        self.master_proxy = master_proxy
        self.execution_features = execution_features
    
        self.current_task_id = None
        self.current_task_execution_record = None
    
        self._lock = Lock()
    
    def abort_task(self, task_id):
        with self._lock:
            if self.current_task_id == task_id:
                self.current_task_execution_record.abort()
            self.current_task_id = None
            self.current_task_execution_record = None
    
    def handle_input(self, input):
        handler = input['handler']

        if handler == 'swi':
            execution_record = SWInterpreterTaskExecutionRecord(input, self)
        else:
            execution_record = SWExecutorTaskExecutionRecord(input, self)

        with self._lock:
            self.current_task_id = uuid.UUID(hex=input['task_id'])
            self.current_task_execution_record = execution_record
        
        cherrypy.log.error("Starting task %s with handler %s" % (str(self.current_task_id), handler), 'TASK', logging.INFO, False)
        try:
            execution_record.execute()
            cherrypy.log.error("Completed task %s with handler %s" % (str(self.current_task_id), handler), 'TASK', logging.INFO, False)
        except:
            cherrypy.log.error("Error in task %s with handler %s" % (str(self.current_task_id), handler), 'TASK', logging.ERROR, True)

        with self._lock:
            self.current_task_id = None
            self.current_task_execution_record = None
            
            
class ReferenceTableEntry:
    
    def __init__(self, reference):
        self.reference = reference
        self.is_dereferenced = False
        self.is_execd = False
        self.is_returned = False
        
    def __repr__(self):
        return 'ReferenceTableEntry(%s, d=%s, e=%s, r=%s)' % (repr(self.reference), repr(self.is_dereferenced), repr(self.is_execd), repr(self.is_returned))
        
class SpawnListEntry:
    
    def __init__(self, id, task_descriptor, continuation=None):
        self.id = id
        self.task_descriptor = task_descriptor
        self.continuation = continuation
        self.ignore = False
    
class SWContinuation:
    
    def __init__(self, task_stmt, context):
        self.task_stmt = task_stmt
        self.current_local_id_index = 0
        self.stack = []
        self.context = context
        self.reference_table = {}
      
    def __repr__(self):
        return "SWContinuation(task_stmt=%s, current_local_id_index=%s, stack=%s, context=%s, reference_table=%s)" % (repr(self.task_stmt), repr(self.current_local_id_index), repr(self.stack), repr(self.context), repr(self.reference_table))

    def create_tasklocal_reference(self, ref):
        id = self.current_local_id_index
        self.current_local_id_index += 1
        self.reference_table[id] = ReferenceTableEntry(ref)
        return SWLocalReference(id)

    def store_tasklocal_reference(self, id, ref):
        """Used when copying references to a spawned continuation."""
        self.reference_table[id] = ReferenceTableEntry(ref)
        self.current_local_id_index = max(self.current_local_id_index, id + 1)
    
    # The following methods capture why we might have blocked on something,
    # for appropriate handling on task loading.
    def mark_as_dereferenced(self, ref):
        self.reference_table[ref.index].is_dereferenced = True
    def is_marked_as_dereferenced(self, id):
        return self.reference_table[id].is_dereferenced
    def mark_as_execd(self, ref):
        self.reference_table[ref.index].is_execd = True
    def is_marked_as_execd(self, id):
        return self.reference_table[id].is_execd
    def mark_as_returned(self, ref):
        self.reference_table[ref.index].is_returned = True
    def is_marked_as_returned(self, id):
        return self.reference_table[id].is_returned
        
    def rewrite_reference(self, id, real_ref):
        self.reference_table[id].reference = real_ref
        
    def resolve_tasklocal_reference_with_index(self, index):
        return self.reference_table[index].reference
    def resolve_tasklocal_reference_with_ref(self, ref):
        return self.reference_table[ref.index].reference

class SWLocalReference:
    """
    A primitive object used in the interpreter, and returned from functions like
    ref() and spawn(). Contains an index into the continuation's reference table,
    which identifies the real reference object.
    """
    
    def __init__(self, index):
        self.index = index
        
    def as_tuple(self):
        return ('local', self.index)

    def __repr__(self):
        return 'SWLocalReference(%d)' % self.index

class SWExecutorTaskExecutionRecord:
    
    def __init__(self, task_descriptor, task_executor):
        self.task_id = uuid.UUID(hex=task_descriptor['task_id'])
        self.expected_outputs = [uuid.UUID(hex=x) for x in task_descriptor['expected_outputs']]
        self.task_executor = task_executor
        self.inputs = task_descriptor['inputs']
        self.executor_name = task_descriptor['handler']
        self.executor = None
        self.is_running = True
    
    def abort(self):
        self.is_running = False
        if self.executor is not None:
            self.executor.abort()
    
    def fetch_executor_args(self, inputs):
        args_ref = None
        parsed_inputs = {}
        
        for local_id, ref in inputs.items():
            if local_id == '_args':
                args_ref = ref
            else:
                parsed_inputs[int(local_id)] = ref
        
        exec_args = self.task_executor.block_store.retrieve_object_for_ref(args_ref, 'pickle')
        
        def args_parsing_mapper(leaf):
            if isinstance(leaf, SWLocalReference):
                return parsed_inputs[leaf.index]
            else:
                return leaf
        
        parsed_args = map_leaf_values(args_parsing_mapper, exec_args)
        return parsed_args
    
    def commit(self):
        commit_bindings = {}
        for i, output_ref in enumerate(self.executor.output_refs):
            output_ref.provenance = SWTaskOutputProvenance(self.task_id, i)
            commit_bindings[output_ref.id] = [output_ref]
        self.task_executor.master_proxy.commit_task(self.task_id, commit_bindings)
    
    def execute(self):        
        try:
            if self.is_running:
                parsed_args = self.fetch_executor_args(self.inputs)
            if self.is_running:
                self.executor = self.task_executor.execution_features.get_executor(self.executor_name, parsed_args, None, self.expected_outputs)
            if self.is_running:
                self.executor.execute(self.task_executor.block_store)
            if self.is_running:
                self.commit()
            else:
                self.task_executor.master_proxy.failed_task(self.task_id)
        except:
            cherrypy.log.error('Error during executor task execution', 'EXEC', logging.ERROR, True)
            self.task_executor.master_proxy.failed_task(self.task_id)
            
class SWInterpreterTaskExecutionRecord:
    
    def __init__(self, task_descriptor, task_executor):
        self.task_id = uuid.UUID(hex=task_descriptor['task_id'])
        self.task_executor = task_executor
        
        self.is_running = True
        self.is_fetching = False
        self.is_interpreting = False
        
        try:
            self.interpreter = SWRuntimeInterpreterTask(task_descriptor, self.task_executor.block_store, self.task_executor.execution_features, self.task_executor.master_proxy)
        except:
            cherrypy.log.error('Error during SWI task creation', 'SWI', logging.ERROR, True)
            self.task_executor.master_proxy.failed_task(self.task_id)            

    def execute(self):
        try:
            if self.is_running:
                self.interpreter.fetch_inputs(self.task_executor.block_store)
            if self.is_running:
                self.interpreter.interpret()
            if self.is_running:
                self.interpreter.spawn_all(self.task_executor.block_store, self.task_executor.master_proxy)
            if self.is_running:
                self.interpreter.commit_result(self.task_executor.block_store, self.task_executor.master_proxy)
            else:
                self.task_executor.master_proxy.failed_task(self.task_id)
        
        except MissingInputException as mie:
            cherrypy.log.error('Missing input during SWI task execution', 'SWI', logging.ERROR, True)
            self.task_executor.master_proxy.failed_task(self.task_id, 'MISSING_INPUT', mie.ref)
                
        except:
            cherrypy.log.error('Error during SWI task execution', 'SWI', logging.ERROR, True)
            self.task_executor.master_proxy.failed_task(self.task_id, 'RUNTIME_EXCEPTION')    

    def abort(self):
        self.is_running = False
        self.interpreter.abort()
        
class SWRuntimeInterpreterTask:
    
    def __init__(self, task_descriptor, block_store, execution_features, master_proxy): # scheduler, task_expr, is_root=False, result_ref_id=None, result_ref_id_list=None, context=None, condvar=None):
        self.task_id = uuid.UUID(hex=task_descriptor['task_id'])
        self.expected_outputs = [uuid.UUID(hex=x) for x in task_descriptor['expected_outputs']]
        self.inputs = task_descriptor['inputs']

        try:
            self.select_result = task_descriptor['select_result']
        except KeyError:
            self.select_result = None
            
        try:
            self.save_continuation = task_descriptor['save_continuation']
        except KeyError:
            self.save_continuation = False
            
        try:
            self.replay_uuid_list = [uuid.UUID(hex=x) for x in task_descriptor['replay_uuids']]
            self.replay_uuids = True
            self.current_uuid_index = 0
        except KeyError:
            self.replay_uuid_list = []
            self.replay_uuids = False

        self.block_store = block_store
        self.execution_features = execution_features

        self.spawn_list = []
        
        self.continuation = None
        self.result = None
        
        self.master_proxy = master_proxy
        
        self.is_running = True
        
        self.current_executor = None
        
    def abort(self):
        self.is_running = False
        if self.current_executor is not None:
            try:
                self.current_executor.abort()
            except:
                pass
            
    def fetch_inputs(self, block_store):
        continuation_ref = None
        parsed_inputs = {}
        
        for local_id, ref in self.inputs.items():
            if local_id == '_cont':
                continuation_ref = ref
            else:
                parsed_inputs[int(local_id)] = ref
        
        self.continuation = block_store.retrieve_object_for_ref(continuation_ref, 'pickle')
        
        for local_id, ref in parsed_inputs.items():
        
            if not self.is_running:
                return
            
            if self.continuation.is_marked_as_dereferenced(local_id):
                if isinstance(ref, SWDataValue):
                    self.continuation.rewrite_reference(local_id, ref)
                else:
                    value = block_store.retrieve_object_for_ref(ref, 'json')
                    self.continuation.rewrite_reference(local_id, SWDataValue(value))
            elif self.continuation.is_marked_as_execd(local_id):
                self.continuation.rewrite_reference(local_id, ref)
            else:
                assert False

    def convert_tasklocal_to_real_reference(self, value):
        if isinstance(value, SWLocalReference):
            return self.continuation.resolve_tasklocal_reference_with_ref(value)
        else:
            return value

    def convert_real_to_tasklocal_reference(self, value):
        if isinstance(value, SWRealReference):
            return self.continuation.create_tasklocal_reference(value)
        else:
            return value

    def create_uuid(self):
        if self.replay_uuids:
            ret = self.replay_uuids[self.current_uuid_index]
            self.current_uuid_index += 1
        else:
            ret = uuid.uuid1()
            self.replay_uuid_list.append(ret)
        return ret

    def interpret(self):
        self.continuation.context.restart()
        task_context = TaskContext(self.continuation.context, self)
        
        task_context.bind_tasklocal_identifier("spawn", LambdaFunction(lambda x: self.spawn_func(x[0], x[1])))
        task_context.bind_tasklocal_identifier("spawn_exec", LambdaFunction(lambda x: self.spawn_exec_func(x[0], x[1], x[2])))
        task_context.bind_tasklocal_identifier("__star__", LambdaFunction(lambda x: self.lazy_dereference(x[0])))
        task_context.bind_tasklocal_identifier("range", LambdaFunction(lambda x: range(x[0],x[1])))
        task_context.bind_tasklocal_identifier("len", LambdaFunction(lambda x: len(x[0])))
        task_context.bind_tasklocal_identifier("exec", LambdaFunction(lambda x: self.exec_func(x[0], x[1], x[2])))
        task_context.bind_tasklocal_identifier("ref", LambdaFunction(lambda x: self.make_reference(x)))
        task_context.bind_tasklocal_identifier("is_future", LambdaFunction(lambda x: self.is_future(x[0])))
        task_context.bind_tasklocal_identifier("is_error", LambdaFunction(lambda x: self.is_error(x[0])))
        task_context.bind_tasklocal_identifier("abort", LambdaFunction(lambda x: self.abort_production(x[0])))
        task_context.bind_tasklocal_identifier("task_details", LambdaFunction(lambda x: self.get_task_details(x[0])))
        task_context.bind_tasklocal_identifier("select", LambdaFunction(lambda x: self.select_func(x[0]) if len(x) == 1 else self.select_func(x[0], x[1])))
        visitor = StatementExecutorVisitor(task_context)
        
        try:
            self.result = visitor.visit(self.continuation.task_stmt, self.continuation.stack, 0)
            
            # XXX: This is for the unusual case that we have a task fragment that runs to completion without returning anything.
            #      Could maybe use an ErrorRef here, but this might not be erroneous if, e.g. the interactive shell is used.
            if self.result is None:
                self.result = SWNullReference()
            
        except SelectException, se:
            
            local_select_group = se.select_group
            timeout = se.timeout
            
            select_group = map(self.continuation.resolve_tasklocal_reference_with_ref, local_select_group)
                        
            cont_task_id = self.create_uuid()
                        
            cont_task_descriptor = {'task_id': str(cont_task_id),
                                    'handler': 'swi',
                                    'inputs': {},
                                    'select_group': select_group,
                                    'select_timeout': timeout,
                                    'expected_outputs': map(str, self.expected_outputs),
                                    'save_continuation': self.save_continuation}
            self.save_continuation = False
            self.spawn_list.append(SpawnListEntry(cont_task_id, cont_task_descriptor, self.continuation))
            
        except ExecutionInterruption, ei:

            # Need to add a continuation task to the spawn list.
            cont_deps = {}
            for index in self.continuation.reference_table.keys():
                if (not isinstance(self.continuation.resolve_tasklocal_reference_with_index(index), SWDataValue)) and \
                   (self.continuation.is_marked_as_dereferenced(index) or self.continuation.is_marked_as_execd(index)):
                    cont_deps[index] = self.continuation.resolve_tasklocal_reference_with_index(index)
            cont_task_id = self.create_uuid()
            cont_task_descriptor = {'task_id': str(cont_task_id),
                                    'handler': 'swi',
                                    'inputs': cont_deps, # _cont will be added at spawn time.
                                    'expected_outputs': map(str, self.expected_outputs),
                                    'save_continuation': self.save_continuation,
                                    'continues_task': str(self.task_id)}
            self.save_continuation = False
            if isinstance(ei, FeatureUnavailableException):
                cont_task_descriptor['require_features'] = [ei.feature_name]
            
            self.spawn_list.append(SpawnListEntry(cont_task_id, cont_task_descriptor, self.continuation))
            return
            
        except MissingInputException as mie:
            print "!!! ERROR: cannot retrieve input: %s" % (repr(mie.ref), )
            raise

        except Exception:
            print "!!! WEIRD EXCEPTION"
            print self.continuation.stack
            self.save_continuation = True
            raise

    def spawn_all(self, block_store, master_proxy):
        current_batch = []
        
        current_index = 0
        while current_index < len(self.spawn_list):
            
            must_wait = False
            
            if self.spawn_list[current_index].ignore:
                current_index += 1
                continue
            
            current_cont = self.spawn_list[current_index].continuation
                
            if must_wait:
                
                if not self.is_running:
                    return
                
                # Fire off the current batch.
                master_proxy.spawn_tasks(self.task_id, current_batch)
                
                # Iterate again on the same index.
                current_batch = []
                continue
                
            else:
                
                # Store the continuation and add it to the task descriptor.
                if current_cont is not None:
                    spawned_cont_id = self.get_spawn_continuation_object_id()
                    cont_url, size_hint = block_store.store_object(current_cont, 'pickle', spawned_cont_id)
                    spawned_cont_ref = SW2_ConcreteReference(spawned_cont_id, SWSpawnedTaskProvenance(self.task_id, current_index), size_hint)
                    spawned_cont_ref.add_location_hint(self.block_store.netloc, ACCESS_SWBS)
                    self.spawn_list[current_index].task_descriptor['inputs']['_cont'] = spawned_cont_ref
            
                # Current task is now ready to be spawned.
                current_batch.append(self.spawn_list[current_index].task_descriptor)
                current_index += 1
            
        if len(current_batch) > 0:
            
            if not self.is_running:
                return
            
            # Fire off the current batch.
            master_proxy.spawn_tasks(self.task_id, current_batch)
            
    def get_spawn_continuation_object_id(self):
        return self.create_uuid()

    def get_continuation_object_id(self):
        return self.create_uuid()

    def commit_result(self, block_store, master_proxy):
        
        if self.result is None:
            if self.save_continuation:
                save_cont_uri, size_hint = self.block_store.store_object(self.continuation, 'pickle', self.get_continuation_object_id())
            else:
                save_cont_uri = None
            master_proxy.commit_task(self.task_id, {}, save_cont_uri, self.replay_uuid_list)
            return
        
        serializable_result = map_leaf_values(self.convert_tasklocal_to_real_reference, self.result)
        commit_bindings = {}

        result_url, size_hint = block_store.store_object(serializable_result, 'json', self.expected_outputs[0])
        if size_hint < 128:
            result_ref = SWDataValue(serializable_result)
        else:
            result_ref = SW2_ConcreteReference(self.expected_outputs[0], SWTaskOutputProvenance(self.task_id, 0), size_hint)
            result_ref.add_location_hint(self.block_store.netloc, ACCESS_SWBS)
            
        commit_bindings[self.expected_outputs[0]] = [result_ref]        
        
        if self.save_continuation:
            save_cont_uri, size_hint = self.block_store.store_object(self.continuation, 'pickle', self.create_uuid())
        else:
            save_cont_uri = None
        
        master_proxy.commit_task(self.task_id, commit_bindings, save_cont_uri, self.replay_uuid_list)

    def build_spawn_continuation(self, spawn_expr, args):
        spawned_task_stmt = ast.Return(ast.SpawnedFunction(spawn_expr, args))
        cont = SWContinuation(spawned_task_stmt, SimpleContext())
        
        
        # Now need to build the reference table for the spawned task.
        local_reference_indices = set()
        
        # Local references in the arguments.
        for leaf in filter(lambda x: isinstance(x, SWLocalReference), all_leaf_values(args)):
            local_reference_indices.add(leaf.index)
            
        # Local references captured in the lambda/function.
        for leaf in filter(lambda x: isinstance(x, SWLocalReference), all_leaf_values(spawn_expr.captured_bindings)):
            local_reference_indices.add(leaf.index)

        if len(local_reference_indices) > 0:
            cont.current_local_id_index = max(local_reference_indices) + 1

        # Actually build the new reference table.
        # TODO: This would be better if we compressed the table, but might take a while.
        #       So let's assume that we won't run out of indices in a normal run :).
        for index in local_reference_indices:
            cont.reference_table[index] = self.continuation.reference_table[index]
        
        return cont

    def spawn_func(self, spawn_expr, args):
        args = map_leaf_values(self.check_no_thunk_mapper, args)        

        # Create new continuation for the spawned function.
        spawned_continuation = self.build_spawn_continuation(spawn_expr, args)
        
        
        
        # Append the new task definition to the spawn list.
        new_task_id = self.create_uuid()
        expected_output_id = self.create_uuid()
        
        # Match up the output with a new tasklocal reference.
        ret = self.continuation.create_tasklocal_reference(SW2_FutureReference(expected_output_id, SWTaskOutputProvenance(new_task_id, 0)))
        
        task_descriptor = {'task_id': str(new_task_id),
                           'handler': 'swi',
                           'inputs': {},
                           'expected_outputs': [str(expected_output_id)] # _cont will be added later
                          }
        
        # TODO: we could visit the spawn expression and try to guess what requirements
        #       and executors we need in here. 
        # TODO: should probably look at dereference wrapper objects in the spawn context
        #       and ship them as inputs.
        
        self.spawn_list.append(SpawnListEntry(new_task_id, task_descriptor, spawned_continuation))

        # Return local reference to the interpreter.
        return ret


    def check_no_thunk_mapper(self, leaf):
        if isinstance(leaf, SWDereferenceWrapper):
            return self.eager_dereference(leaf.ref)
        else:
            return leaf
   
    def spawn_exec_func(self, executor_name, exec_args, num_outputs):
        
        new_task_id = self.create_uuid()
        expected_output_ids = [self.create_uuid() for i in range(num_outputs)]
        ret = [self.continuation.create_tasklocal_reference(SW2_FutureReference(expected_output_ids[i], SWTaskOutputProvenance(new_task_id, i))) for i in range(num_outputs)]
        inputs = {}
        
        args = map_leaf_values(self.check_no_thunk_mapper, exec_args)

        def args_check_mapper(leaf):
            if isinstance(leaf, SWLocalReference):
                real_ref = self.continuation.resolve_tasklocal_reference_with_ref(leaf)
                if isinstance(real_ref, SWFutureReference):
                    i = len(inputs)
                    inputs[i] = real_ref
                    ret = SWLocalReference(i)
                    return ret
                else:
                    return real_ref
            return leaf
        
        transformed_args = map_leaf_values(args_check_mapper, args)
        args_id = self.create_uuid()
        args_url, size_hint = self.block_store.store_object(transformed_args, 'pickle', args_id)
        args_ref = SW2_ConcreteReference(args_id, SWNoProvenance(), size_hint)
        args_ref.add_location_hint(self.block_store.netloc, ACCESS_SWBS)
        
        inputs['_args'] = args_ref
        
        task_descriptor = {'task_id': str(new_task_id),
                           'handler': executor_name, 
                           'inputs': inputs,
                           'expected_outputs': map(str, expected_output_ids)}
        
        self.spawn_list.append(SpawnListEntry(new_task_id, task_descriptor))
        
        return ret
    
    def exec_func(self, executor_name, args, num_outputs):
        
        real_args = map_leaf_values(self.check_no_thunk_mapper, args)

        output_ids = map(self.create_uuid, range(0, num_outputs))

        self.current_executor = self.execution_features.get_executor(executor_name, real_args, self.continuation, output_ids)
        self.current_executor.execute(self.block_store)
        ret = map(self.continuation.create_tasklocal_reference, self.current_executor.output_refs)
        self.current_executor = None
        return ret

    def make_reference(self, urls):
        return self.continuation.create_tasklocal_reference(SWURLReference(urls))

    def lazy_dereference(self, ref):
        self.continuation.mark_as_dereferenced(ref)
        return SWDereferenceWrapper(ref)
        
    def eager_dereference(self, ref):
        real_ref = self.continuation.resolve_tasklocal_reference_with_ref(ref)
        if isinstance(real_ref, SWDataValue):
            return map_leaf_values(self.convert_real_to_tasklocal_reference, real_ref.value)
        elif isinstance(real_ref, SWURLReference):
            value = self.block_store.retrieve_object_for_ref(real_ref, 'json')
            dv_ref = SWDataValue(value)
            self.continuation.rewrite_reference(ref.index, dv_ref)
            return map_leaf_values(self.convert_real_to_tasklocal_reference, value)
        else:
            self.continuation.mark_as_dereferenced(ref)
            raise ReferenceUnavailableException(ref, self.continuation)

    def is_future(self, ref):
        real_ref = self.continuation.resolve_tasklocal_reference_with_ref(ref)
        return isinstance(real_ref, SWFutureReference)

    def is_error(self, ref):
        real_ref = self.continuation.resolve_tasklocal_reference_with_ref(ref)
        return isinstance(real_ref, SWErrorReference)

    def abort_production(self, ref):
        raise
        #real_ref = self.continuation.resolve_tasklocal_reference_with_ref(ref)
        #if isinstance(real_ref, SWLocalFutureReference):
        #    self.spawn_list[real_ref.spawn_list_index].ignore = True
        #elif isinstance(real_ref, SWGlobalFutureReference):
        #    self.master_proxy.abort_production_of_output(real_ref)
        #return True
    
    def get_task_details(self, ref):
        raise
        #real_ref = self.continuation.resolve_tasklocal_reference_with_ref(ref)
        #if isinstance(real_ref, SWGlobalFutureReference):
        #    return self.master_proxy.get_task_details_for_future(ref)
        #else:
        #    return {}

    def select_func(self, select_group, timeout=None):
        if self.select_result is not None:
            return self.select_result
        else:
            raise SelectException(select_group, timeout)
