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
Created on 15 Apr 2010

@author: dgm36
'''

class LocalMasterProxy:
    
    def __init__(self, task_pool, block_store, global_name_directory, worker_pool):
        self.task_pool = task_pool
        self.block_store = block_store
        self.global_name_directory = global_name_directory
        self.worker_pool = worker_pool
    
    def publish_global_refs(self, global_id, refs):
        self.global_name_directory.add_refs_for_id(global_id, refs)
        
    def spawn_tasks(self, parent_task_id, task_descriptors):
        spawn_result_ids = []
        for task in task_descriptors:
            try:
                expected_outputs = task['expected_outputs']
            except KeyError:
                try:
                    num_outputs = task['num_outputs']
                    expected_outputs = map(lambda x: self.global_name_directory.create_global_id(), range(0, num_outputs))
                except:
                    expected_outputs = self.global_name_directory.create_global_id()
                task['expected_outputs'] = expected_outputs

            self.task_pool.add_task(task)
            spawn_result_ids.append(expected_outputs) 

        return spawn_result_ids

    def commit_task(self, task_id, commit_bindings):
        for global_id, urls in commit_bindings.items():
            self.data_store.add_urls_for_id(global_id, urls)
        return True
                           
    def failed_task(self, task_id):
        raise Exception()
        
    def get_task_descriptor_for_future(self, ref):
        task_id = self.global_name_directory.get_task_for_id()
        task = self.task_pool.get_task_by_id(task_id)
        task_descriptor = task.as_descriptor()
        if task.worker is not None:
            task_descriptor['worker'] = task.worker.as_descriptor()
        return task_descriptor