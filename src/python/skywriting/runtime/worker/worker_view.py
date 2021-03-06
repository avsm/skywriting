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
from skywriting.runtime.executors import kill_all_running_children

'''
Created on 8 Feb 2010

@author: dgm36
'''
from cherrypy.lib.static import serve_file
from skywriting.runtime.block_store import json_decode_object_hook
import sys
import simplejson
import cherrypy
import os
import time

class WorkerRoot:
    
    def __init__(self, worker):
        self.worker = worker
        self.master = RegisterMasterRoot(worker)
        self.task = TaskRoot(worker)
        self.data = DataRoot(worker.block_store)
        self.features = FeaturesRoot(worker.execution_features)
        self.kill = KillRoot()
        self.log = LogRoot(worker)
        self.upload = UploadRoot(worker.upload_manager)
    
    @cherrypy.expose
    def index(self):
        return simplejson.dumps(self.worker.id)

class KillRoot:
    
    def __init__(self):
        pass
    
    @cherrypy.expose
    def index(self):
        kill_all_running_children()
        sys.exit(0)

class RegisterMasterRoot:
    
    def __init__(self, worker):
        self.worker = worker
            
    @cherrypy.expose
    def index(self):
        if cherrypy.request.method == 'POST':
            master_details = simplejson.loads(cherrypy.request.body.read())
            self.worker.set_master(master_details)
        elif cherrypy.request.method == 'GET':
            return simplejson.dumps(self.worker.master_proxy.get_master_details())
        else:
            raise cherrypy.HTTPError(405)
    
class TaskRoot:
    
    def __init__(self, worker):
        self.worker = worker
        
    @cherrypy.expose
    def index(self):
        if cherrypy.request.method == 'POST':
            task_descriptor = simplejson.loads(cherrypy.request.body.read(), object_hook=json_decode_object_hook)
            if task_descriptor is not None:
                self.worker.submit_task(task_descriptor)
                return
        raise cherrypy.HTTPError(405)
    
    @cherrypy.expose
    def default(self, task_id, action):
        real_id = task_id
        if action == 'abort':
            if cherrypy.request.method == 'POST':
                self.worker.abort_task(real_id)
            else:
                raise cherrypy.HTTPError(405)
        else:
            raise cherrypy.HTTPError(404)
    
    # TODO: Add some way of checking up on the status of a running task.
    #       This should grow to include a way of getting the present activity of the task
    #       and a way of setting breakpoints.
    #       ...and a way of killing the task.
    #       Ideally, we should create a task view (Root) for each running task.    

class LogRoot:

    def __init__(self, worker):
        self.worker = worker

    @cherrypy.expose
    def wait_after(self, event_count):
        if cherrypy.request.method == 'POST':
            try:
                self.worker.await_log_entries_after(event_count)
                return simplejson.dumps({})
            except Exception as e:
                return simplejson.dumps({"error": repr(e)})
        else:
            raise cherrypy.HTTPError(405)

    @cherrypy.expose
    def index(self, first_event, last_event):
        if cherrypy.request.method == 'GET':
            events = self.worker.get_log_entries(int(first_event), int(last_event))
            return simplejson.dumps([{"time": repr(t), "event": e} for (t, e) in events])
        else:
            raise cherrypy.HTTPError(405)

class DataRoot:
    
    def __init__(self, block_store):
        self.block_store = block_store
        
    @cherrypy.expose
    def default(self, id):
        safe_id = id
        if cherrypy.request.method == 'GET':
            is_streaming, filename = self.block_store.maybe_streaming_filename(safe_id)
            if is_streaming:
                cherrypy.response.headers['Pragma'] = 'streaming'
            try:
                response_body = serve_file(filename)
                return response_body
            except cherrypy.HTTPError as he:
                # The streaming file might have been deleted between calls to maybe_streaming_filename
                # and serve_file. Try again, because this time the non-streaming filename should be
                # available.
                if he.status == 404:
                    is_streaming, filename = self.block_store.maybe_streaming_filename(safe_id)
                    assert not is_streaming
                    cherrypy.response.headers.pop(['Pragma'], None)
                    return serve_file(filename)
                else:
                    raise
        elif cherrypy.request.method == 'POST':
            url = self.block_store.store_raw_file(cherrypy.request.body, safe_id)
            return simplejson.dumps(url)
        else:
            raise cherrypy.HTTPError(405)

    @cherrypy.expose
    def index(self):
        if cherrypy.request.method == 'POST':
            id = self.block_store.allocate_new_id()
            self.block_store.store_raw_file(cherrypy.request.body, id)
            return simplejson.dumps(id)
        elif cherrypy.request.method == 'GET':
            return serve_file(self.block_store.generate_block_list_file())
        else:
            raise cherrypy.HTTPError(405)
        
    # TODO: Also might investigate a way for us to have a spanning tree broadcast
    #       for common files.
    
class UploadRoot:
    
    def __init__(self, upload_manager):
        self.upload_manager = upload_manager
        
    @cherrypy.expose
    def default(self, id, start=None):
        if cherrypy.request.method != 'POST':
            raise cherrypy.HTTPError(405)
        if start is None:
            #upload_descriptor = simplejson.loads(cherrypy.request.body.read(), object_hook=json_decode_object_hook)
            self.upload_manager.start_upload(id)#, upload_descriptor)
        elif start == 'commit':
            self.upload_manager.commit_upload(id)
        else:
            start_index = int(start)
            self.upload_manager.handle_chunk(id, start_index, cherrypy.request.body)
    
class FeaturesRoot:
    
    def __init__(self, execution_features):
        self.execution_features = execution_features
    
    @cherrypy.expose
    def index(self):
        return simplejson.dumps(self.execution_features.all_features())
