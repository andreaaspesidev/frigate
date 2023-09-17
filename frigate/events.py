import datetime
import json
import logging
import os
import queue
import subprocess as sp
import multiprocessing as mp
import threading
import time
from collections import defaultdict
from pathlib import Path

import psutil
import shutil

from frigate.config import FrigateConfig
from frigate.const import RECORD_DIR, CLIPS_DIR, CACHE_DIR
from frigate.http import event
from frigate.models import Event

from peewee import fn

logger = logging.getLogger(__name__)



class EventProcessor(threading.Thread):
    def __init__(self, config, camera_processes, event_queue, event_processed_queue, stop_event, video_queue):
        threading.Thread.__init__(self)
        self.name = 'event_processor'
        self.config = config
        self.camera_processes = camera_processes    # Processes responsible for capturing camera cache
        self.cached_clips = {}                      # Clips currenlty in the cache, that is shared among cameras
        self.event_queue = event_queue              # Queue where events arrive from
        self.event_processed_queue = event_processed_queue  # Queue of final events. When put here, the event definitely closes
        self.stop_event = stop_event
        self.video_queue = video_queue

        self.events_in_process = {}                         # camera => event_data
        self.events_in_process_lock = threading.Lock()      # Lock for syncr. events_in_process
        self.clips_queue = mp.Queue()                       # Threadsafe queue for the clips assembler
        self.clips_threading = {}                           # camera => thread

    def run(self):
        while True:
            # Is termination is required, exit
            if self.stop_event.is_set():
                logger.info(f"Exiting event processor...")
                break

            # finalize pending events
            self.finalize_events()

            try:
                event_type, camera, event_data = self.event_queue.get(timeout=10)
            except queue.Empty:
                if not self.stop_event.is_set():
                    self.refresh_cache()    # in any case, keep under control cache
                continue

            # new event acquired
            logger.debug(f"Event received: {event_type} {camera} {event_data['id']}")

            # update timings
            if self.update_timings(camera, event_type, event_data):
                # new camera ready to wait for the events, assign a new thread
                clips_config = self.config.cameras[camera].clips
                camera_thread = threading.Thread(target=self.create_clip, args=(camera, clips_config.pre_capture, clips_config.post_capture, ))
                camera_thread.daemon = True
                camera_thread.start()
                self.clips_threading[camera] = camera_thread
                pass
            
            # delete old clips to avoid filling up cache
            self.refresh_cache()

    def should_create_clip(self, camera, event_data):
        if event_data['false_positive']:
            return False
        
        # if there are required zones and there is no overlap
        required_zones = self.config.cameras[camera].clips.required_zones
        if len(required_zones) > 0 and not set(event_data['entered_zones']) & set(required_zones):
            logger.debug(f"Not creating clip for {event_data['id']} because it did not enter required zones")
            return False

        return True

    def update_timings(self, camera, event_type, event_data):
        with self.events_in_process_lock:
            if camera in self.events_in_process:
                # update timings of the active event
                if event_type == 'start':
                    # new linked event arrived, update the end time
                    if event_data['start_time'] > self.events_in_process[camera]['end_time']:
                        # update the end of this event
                        self.events_in_process[camera]['end_time'] = event_data['start_time']
                        logger.debug(f"Postponed end of event {self.events_in_process[camera]['id']}")
                elif event_type == 'end':
                        # new linked event ended, update the end time
                    if event_data['end_time'] > self.events_in_process[camera]['end_time']:
                        # update the end of this event
                        self.events_in_process[camera]['end_time'] = event_data['end_time']
                        logger.debug(f"Postponed end of event {self.events_in_process[camera]['id']}")
            else:
                # new event arrived, prepare for clip if needed
                if event_type == 'end':
                    clips_config = self.config.cameras[camera].clips
                    if self.should_create_clip(camera, event_data):
                        if clips_config.enabled and (clips_config.objects is None or event_data['label'] in clips_config.objects):
                            self.events_in_process[camera] = event_data     # put event in process
                            return True # skip and go to record

        # if the event carries a snapshot, persist the event in any case
        if event_type == 'end':
            if event_data['has_snapshot']:
                Event.create(
                    id=event_data['id'],
                    label=event_data['label'],
                    camera=camera,
                    start_time=event_data['start_time'],
                    end_time=event_data['end_time'],
                    top_score=event_data['top_score'],
                    false_positive=event_data['false_positive'],
                    zones=list(event_data['entered_zones']),
                    thumbnail=event_data['thumbnail'],
                    has_clip=False,
                    has_snapshot=True,
                )
            # in any case, mark the event as ended
            self.event_processed_queue.put((event_data['id'], camera))
        return False      

    def finalize_events(self):
        # check for finished clips
        try:
            event_data = self.clips_queue.get(timeout=2)
        except queue.Empty:
            return
        
        # finalize the event with the clip ready
        if event_data['has_clip']:
            # persist the event
            Event.create(
                id=event_data['id'],
                label=event_data['label'],
                camera=event_data['camera'],
                start_time=event_data['start_time'],
                end_time=event_data['end_time'],
                top_score=event_data['top_score'],
                false_positive=event_data['false_positive'],
                zones=list(event_data['entered_zones']),
                thumbnail=event_data['thumbnail'],
                has_clip=True,
                has_snapshot=event_data['has_snapshot'],
            )
        # in any case, mark as completed
        with self.events_in_process_lock:
            del self.events_in_process[event_data['camera']]

        del self.clips_threading[event_data['camera']]
        self.event_processed_queue.put((event_data['id'], event_data['camera']))

    def refresh_cache(self):
        cached_files = os.listdir(CACHE_DIR)

        files_in_use = []
        for process in psutil.process_iter():
            try:
                if process.name() != 'ffmpeg':
                    continue

                flist = process.open_files()
                if flist:
                    for nt in flist:
                        if nt.path.startswith(CACHE_DIR):
                            files_in_use.append(nt.path.split('/')[-1])
            except:
                continue

        for f in cached_files:
            if f in files_in_use or f in self.cached_clips:
                continue

            camera = '-'.join(f.split('-')[:-1])
            start_time = datetime.datetime.strptime(f.split('-')[-1].split('.')[0], '%Y%m%d%H%M%S')
        
            ffprobe_cmd = " ".join([
                'ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration',
                '-of',
                'default=noprint_wrappers=1:nokey=1',
                f"{os.path.join(CACHE_DIR,f)}"
            ])
            p = sp.Popen(ffprobe_cmd, stdout=sp.PIPE, shell=True)
            (output, err) = p.communicate()
            p_status = p.wait()
            if p_status == 0:
                duration = float(output.decode('utf-8').strip())
            else:
                logger.info(f"bad file: {f}")
                os.remove(os.path.join(CACHE_DIR,f))
                continue

            self.cached_clips[f] = {
                'path': f,
                'camera': camera,
                'start_time': start_time.timestamp(),
                'duration': duration
            }
        
        # if we are still using more than 90% of the cache, proactively cleanup
        cache_usage = shutil.disk_usage("/tmp/cache")
        if cache_usage.used/cache_usage.total > .9 and cache_usage.free < 200000000 and len(self.cached_clips) > 0:
            logger.debug("Proactively cleaning up the cache...")
            while cache_usage.used/cache_usage.total > .9:
                oldest_clip = min(self.cached_clips.values(), key=lambda x:x['start_time'])
                del self.cached_clips[oldest_clip['path']]
                logger.debug(f"Cleaning up cached file {oldest_clip['path']}")
                os.remove(os.path.join(CACHE_DIR,oldest_clip['path']))
                cache_usage = shutil.disk_usage("/tmp/cache")

    def create_clip(self, camera, pre_capture, post_capture):
        def get_event_data():
            with self.events_in_process_lock:
                return self.events_in_process[camera]
        
        # get all clips from the camera with the event sorted
        sorted_clips = sorted([c for c in self.cached_clips.values() if c['camera'] == camera], key = lambda i: i['start_time'])

        # monitor first clip and last clip in time
        # if last clip covers the total time, create. But if the first clip is outside the window then this means that the cache is full
        # in this case we use all the available cache
        while len(sorted_clips) == 0:
            logger.debug(f"Waiting starting clip for {camera}...")
            time.sleep(5)
            if self.stop_event.is_set():
                return
            # get all clips from the camera with the event sorted
            sorted_clips = sorted([c for c in self.cached_clips.values() if c['camera'] == camera], key = lambda i: i['start_time'])

        # if the first clip is not right, means the system is still starting. Ignore this clip
        if get_event_data()['start_time']-pre_capture < sorted_clips[0]['start_time']:
            self.clip_ready_to_be_finalized(camera, False)
            logger.warn("Ignored clip creation as cache is not full yet!")
            return

        # else we start looking for the end clip, while monitoring the first clip does not exit the cache
        while   get_event_data()['start_time']-pre_capture >= sorted_clips[0]['start_time'] and \
                sorted_clips[-1]['start_time'] + sorted_clips[-1]['duration'] < get_event_data()['end_time'] + post_capture:
            logger.debug(f"Waiting clips for {camera}: missing {get_event_data()['end_time'] + post_capture - sorted_clips[-1]['start_time'] + sorted_clips[-1]['duration']} s ...")
            time.sleep(5)
            if self.stop_event.is_set():
                return
            # get all clips from the camera with the event sorted
            sorted_clips = sorted([c for c in self.cached_clips.values() if c['camera'] == camera], key = lambda i: i['start_time'])
        
        if get_event_data()['start_time']-pre_capture < sorted_clips[0]['start_time']:
            # we lost cache elements, warning but then use all cache
            logger.warn(f"Cache too small. The final clip for {camera} using what's left")
            playlist_start = sorted_clips[0]['start_time']
        else:
            playlist_start = get_event_data()['start_time']-pre_capture
        
        playlist_end = get_event_data()['end_time']+post_capture
        playlist_lines = []
        for clip in sorted_clips:
            # clip ends before playlist start time, skip
            if clip['start_time']+clip['duration'] < playlist_start:
                continue
            # clip starts after playlist ends, finish
            if clip['start_time'] > playlist_end:
                break
            playlist_lines.append(f"file '{os.path.join(CACHE_DIR,clip['path'])}'")
            # if this is the starting clip, add an inpoint
            if clip['start_time'] < playlist_start:
                playlist_lines.append(f"inpoint {int(playlist_start-clip['start_time'])}")
            # if this is the ending clip, add an outpoint
            if clip['start_time']+clip['duration'] > playlist_end:
                playlist_lines.append(f"outpoint {int(playlist_end-clip['start_time'])}")

        clip_name = f"{camera}-{get_event_data()['id']}"
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-protocol_whitelist',
            'pipe,file',
            '-f',
            'concat',
            '-safe',
            '0',
            '-i',
            '-',
            '-c',
            'copy',
            '-movflags',
            '+faststart',
            f"{os.path.join(CLIPS_DIR, clip_name)}.mp4"
        ]

        logger.debug(f"Generating clip for {camera} ...")

        p = sp.run(ffmpeg_cmd, input="\n".join(playlist_lines), encoding='ascii', capture_output=True)
        if p.returncode != 0:
            logger.error(p.stderr)
            self.clip_ready_to_be_finalized(camera, False)
            return
        logger.debug(f"Generated clip for {camera}!")
        # Add to queue
        self.video_queue.put((camera, f"{os.path.join(CLIPS_DIR, clip_name)}.mp4"))
        self.clip_ready_to_be_finalized(camera, True)

    def clip_ready_to_be_finalized(self, camera, has_clip):
        with self.events_in_process_lock:
            self.events_in_process[camera]['has_clip'] = has_clip       # Mark as completed
            self.clips_queue.put_nowait(self.events_in_process[camera]) # Set to be finalized

# class EventProcessor(threading.Thread):
#    def __init__(self, config, camera_processes, event_queue, event_processed_queue, stop_event, video_queue):
#        threading.Thread.__init__(self)
#        self.name = 'event_processor'
#        self.config = config
#        self.camera_processes = camera_processes
#        self.cached_clips = {}     # Clips currenlty in the cache, that is shared among cameras
#       self.event_queue = event_queue  # Queue where events arrive from
#        self.event_processed_queue = event_processed_queue  # Queue of final events. When put here, the event definitely closes
#        self.events_in_process = {} # Events current being processed
#        self.stop_event = stop_event
#        self.video_queue = video_queue

#    def should_create_clip(self, camera, event_data):
#        if event_data['false_positive']:
#            return False
#
#        # if there are required zones and there is no overlap
#        required_zones = self.config.cameras[camera].clips.required_zones
#        if len(required_zones) > 0 and not set(event_data['entered_zones']) & set(required_zones):
#            logger.debug(f"Not creating clip for {event_data['id']} because it did not enter required zones")
#            return False
#
#        return True
    
#    def refresh_cache(self):  # Deletes old clips to avoid filling up the cache
#        cached_files = os.listdir(CACHE_DIR)
#
#        files_in_use = []
#        for process in psutil.process_iter():
#            try:
#                if process.name() != 'ffmpeg':
#                    continue
#
#                flist = process.open_files()
#                if flist:
#                    for nt in flist:
#                        if nt.path.startswith(CACHE_DIR):
#                            files_in_use.append(nt.path.split('/')[-1])
#            except:
#                continue
#
#        for f in cached_files:
#            if f in files_in_use or f in self.cached_clips:
#                continue
#
#            camera = '-'.join(f.split('-')[:-1])
#            start_time = datetime.datetime.strptime(f.split('-')[-1].split('.')[0], '%Y%m%d%H%M%S')
        
#            ffprobe_cmd = " ".join([
#                'ffprobe',
#                '-v',
#                'error',
#                '-show_entries',
#                'format=duration',
#                '-of',
#                'default=noprint_wrappers=1:nokey=1',
#                f"{os.path.join(CACHE_DIR,f)}"
#            ])
#            p = sp.Popen(ffprobe_cmd, stdout=sp.PIPE, shell=True)
#            (output, err) = p.communicate()
#            p_status = p.wait()
#            if p_status == 0:
#                duration = float(output.decode('utf-8').strip())
#            else:
#                logger.info(f"bad file: {f}")
#                os.remove(os.path.join(CACHE_DIR,f))
#                continue

#            self.cached_clips[f] = {
#                'path': f,
#                'camera': camera,
#                'start_time': start_time.timestamp(),
#                'duration': duration
#            }
        
        # if we are still using more than 90% of the cache, proactively cleanup
#       cache_usage = shutil.disk_usage("/tmp/cache")
#        if cache_usage.used/cache_usage.total > .9 and cache_usage.free < 200000000 and len(self.cached_clips) > 0:
#            logger.debug("Proactively cleaning up the cache...")
#            while cache_usage.used/cache_usage.total > .9:
#                oldest_clip = min(self.cached_clips.values(), key=lambda x:x['start_time'])
#                del self.cached_clips[oldest_clip['path']]
#                logger.debug(f"Cleaning up cached file {oldest_clip['path']}")
#                os.remove(os.path.join(CACHE_DIR,oldest_clip['path']))
#                cache_usage = shutil.disk_usage("/tmp/cache")

#    def create_clip(self, camera, event_data, pre_capture, post_capture):
#        # get all clips from the camera with the event sorted
#        sorted_clips = sorted([c for c in self.cached_clips.values() if c['camera'] == camera], key = lambda i: i['start_time'])

#        # monitor first clip and last clip in time
#        # if last clip covers the total time, create. But if the first clip is outside the window then this means that the cache is full
#        # in this case we use all the available cache
#        while len(sorted_clips) == 0:
#            logger.debug(f"Waiting starting clip for {camera}...")
#            time.sleep(5)
#            self.refresh_cache()
#            # get all clips from the camera with the event sorted
#            sorted_clips = sorted([c for c in self.cached_clips.values() if c['camera'] == camera], key = lambda i: i['start_time'])

#        # if the first clip is not right, means the system is still starting. Ignore this clip
#        if event_data['start_time']-pre_capture < sorted_clips[0]['start_time']:
#            logger.warn("Ignored clip creation as cache is not full yet!")
#            return False

#        # else we start looking for the end clip, while monitoring the first clip does not exit the cache
#        while   event_data['start_time']-pre_capture >= sorted_clips[0]['start_time'] and \
#                sorted_clips[-1]['start_time'] + sorted_clips[-1]['duration'] < event_data['end_time'] + post_capture:
#            logger.debug(f"Waiting clips for {camera}...")
#            time.sleep(5)
#            self.refresh_cache()
#            # get all clips from the camera with the event sorted
#            sorted_clips = sorted([c for c in self.cached_clips.values() if c['camera'] == camera], key = lambda i: i['start_time'])
        
#        if event_data['start_time']-pre_capture < sorted_clips[0]['start_time']:
#            # we lost cache elements, warning but then use all cache
#            logger.warn(f"Cache too small. The final clip for {camera} using what's left")
#            playlist_start = sorted_clips[0]['start_time']
#        else:
#            playlist_start = event_data['start_time']-pre_capture
        
#        playlist_end = event_data['end_time']+post_capture
#        playlist_lines = []
#        for clip in sorted_clips:
#            # clip ends before playlist start time, skip
#            if clip['start_time']+clip['duration'] < playlist_start:
#                continue
#            # clip starts after playlist ends, finish
#            if clip['start_time'] > playlist_end:
#                break
#            playlist_lines.append(f"file '{os.path.join(CACHE_DIR,clip['path'])}'")
#            # if this is the starting clip, add an inpoint
#            if clip['start_time'] < playlist_start:
#                playlist_lines.append(f"inpoint {int(playlist_start-clip['start_time'])}")
#            # if this is the ending clip, add an outpoint
#            if clip['start_time']+clip['duration'] > playlist_end:
#                playlist_lines.append(f"outpoint {int(playlist_end-clip['start_time'])}")

#        clip_name = f"{camera}-{event_data['id']}"
#        ffmpeg_cmd = [
#            'ffmpeg',
#            '-y',
#            '-protocol_whitelist',
#            'pipe,file',
#            '-f',
#            'concat',
#            '-safe',
#            '0',
#            '-i',
#            '-',
#            '-c',
#            'copy',
#            '-movflags',
#            '+faststart',
#            f"{os.path.join(CLIPS_DIR, clip_name)}.mp4"
#        ]

#        p = sp.run(ffmpeg_cmd, input="\n".join(playlist_lines), encoding='ascii', capture_output=True)
#        if p.returncode != 0:
#            logger.error(p.stderr)
#            return False
#        # Add to queue
#        self.video_queue.put((camera, f"{os.path.join(CLIPS_DIR, clip_name)}.mp4"))
#        return True

#    def run(self):
#        while True:
#            if self.stop_event.is_set():
#                logger.info(f"Exiting event processor...")
#                break

#            try:
#                event_type, camera, event_data = self.event_queue.get(timeout=10)
#            except queue.Empty:
#                if not self.stop_event.is_set():
#                    self.refresh_cache()
#                continue

#            logger.debug(f"Event received: {event_type} {camera} {event_data['id']}")
#            self.refresh_cache()

#            if event_type == 'start':
#                self.events_in_process[event_data['id']] = event_data

#            if event_type == 'end':
#                clips_config = self.config.cameras[camera].clips

#                clip_created = False
#                if self.should_create_clip(camera, event_data):
#                    if clips_config.enabled and (clips_config.objects is None or event_data['label'] in clips_config.objects):
#                        clip_created = self.create_clip(camera, event_data, clips_config.pre_capture, clips_config.post_capture)
                
#                if clip_created or event_data['has_snapshot']:
#                    Event.create(
#                        id=event_data['id'],
#                        label=event_data['label'],
#                        camera=camera,
#                        start_time=event_data['start_time'],
#                        end_time=event_data['end_time'],
#                        top_score=event_data['top_score'],
#                        false_positive=event_data['false_positive'],
#                        zones=list(event_data['entered_zones']),
#                        thumbnail=event_data['thumbnail'],
#                        has_clip=clip_created,
#                        has_snapshot=event_data['has_snapshot'],
#                    )
#                del self.events_in_process[event_data['id']]
#                self.event_processed_queue.put((event_data['id'], camera))

class EventCleanup(threading.Thread):
    def __init__(self, config: FrigateConfig, stop_event):
        threading.Thread.__init__(self)
        self.name = 'event_cleanup'
        self.config = config
        self.stop_event = stop_event
        self.camera_keys = list(self.config.cameras.keys())

    def expire(self, media):
        ## Expire events from unlisted cameras based on the global config
        if media == 'clips':
            retain_config = self.config.clips.retain
            file_extension = 'mp4'
            update_params = {'has_clip': False}
        else:
            retain_config = self.config.snapshots.retain
            file_extension = 'jpg'
            update_params = {'has_snapshot': False}
        
        distinct_labels = (Event.select(Event.label)
                    .where(Event.camera.not_in(self.camera_keys))
                    .distinct())
        
        # loop over object types in db
        for l in distinct_labels:
            # get expiration time for this label
            expire_days = retain_config.objects.get(l.label, retain_config.default)
            expire_after = (datetime.datetime.now() - datetime.timedelta(days=expire_days)).timestamp()
            # grab all events after specific time
            expired_events = (
                Event.select()
                    .where(Event.camera.not_in(self.camera_keys), 
                        Event.start_time < expire_after, 
                        Event.label == l.label)
            )
            # delete the media from disk
            for event in expired_events:
                media_name = f"{event.camera}-{event.id}"
                media = Path(f"{os.path.join(CLIPS_DIR, media_name)}.{file_extension}")
                media.unlink(missing_ok=True)
                # delete also original clip if exists
                original_media = VideoConverter.get_original_backup_file(f"{os.path.join(CLIPS_DIR, media_name)}.{file_extension}")
                Path(original_media).unlink(missing_ok=True)
            # update the clips attribute for the db entry
            update_query = (
                Event.update(update_params)
                    .where(Event.camera.not_in(self.camera_keys), 
                        Event.start_time < expire_after, 
                        Event.label == l.label)
            )
            update_query.execute()

        ## Expire events from cameras based on the camera config
        for name, camera in self.config.cameras.items():
            if media == 'clips':
                retain_config = camera.clips.retain
            else:
                retain_config = camera.snapshots.retain
            # get distinct objects in database for this camera
            distinct_labels = (Event.select(Event.label)
                    .where(Event.camera == name)
                    .distinct())

            # loop over object types in db
            for l in distinct_labels:
                # get expiration time for this label
                expire_days = retain_config.objects.get(l.label, retain_config.default)
                expire_after = (datetime.datetime.now() - datetime.timedelta(days=expire_days)).timestamp()
                # grab all events after specific time
                expired_events = (
                    Event.select()
                        .where(Event.camera == name, 
                            Event.start_time < expire_after, 
                            Event.label == l.label)
                )
                # delete the grabbed clips from disk
                for event in expired_events:
                    media_name = f"{event.camera}-{event.id}"
                    media = Path(f"{os.path.join(CLIPS_DIR, media_name)}.{file_extension}")
                    media.unlink(missing_ok=True)
                    # delete also original clip if exists
                    original_media = VideoConverter.get_original_backup_file(f"{os.path.join(CLIPS_DIR, media_name)}.{file_extension}")
                    Path(original_media).unlink(missing_ok=True)
                # update the clips attribute for the db entry
                update_query = (
                    Event.update(update_params)
                        .where( Event.camera == name, 
                            Event.start_time < expire_after, 
                            Event.label == l.label)
                )
                update_query.execute()

    def purge_duplicates(self):
        duplicate_query = """with grouped_events as (
          select id,
            label, 
            camera, 
          	has_snapshot,
          	has_clip,
          	row_number() over (
              partition by label, camera, round(start_time/5,0)*5
              order by end_time-start_time desc
            ) as copy_number
          from event
        )

        select distinct id, camera, has_snapshot, has_clip from grouped_events 
        where copy_number > 1;"""

        duplicate_events = Event.raw(duplicate_query)
        for event in duplicate_events:
            logger.debug(f"Removing duplicate: {event.id}")
            media_name = f"{event.camera}-{event.id}"
            if event.has_snapshot:
                media = Path(f"{os.path.join(CLIPS_DIR, media_name)}.jpg")
                media.unlink(missing_ok=True)
            if event.has_clip:
                media = Path(f"{os.path.join(CLIPS_DIR, media_name)}.mp4")
                media.unlink(missing_ok=True)
                # Delete also original if exists
                original_media = VideoConverter.get_original_backup_file(f"{os.path.join(CLIPS_DIR, media_name)}.mp4")
                original_media.unlink(missing_ok=True)

        (Event.delete()
            .where( Event.id << [event.id for event in duplicate_events] )
            .execute())
    
    def run(self):
        counter = 0
        while(True):
            if self.stop_event.is_set():
                logger.info(f"Exiting event cleanup...")
                break

            # only expire events every 5 minutes, but check for stop events every 10 seconds
            time.sleep(10)
            counter = counter + 1
            if counter < 30:
                continue
            counter = 0

            self.expire('clips')
            self.expire('snapshots')
            self.purge_duplicates()

            # drop events from db where has_clip and has_snapshot are false
            delete_query = (
                Event.delete()
                    .where( Event.has_clip == False, 
                        Event.has_snapshot == False)
            )
            delete_query.execute()

class VideoConverter(threading.Thread):
    def __init__(self, config: FrigateConfig, stop_event, video_queue):
        threading.Thread.__init__(self)
        self.name = 'video_converter'
        self.config = config
        self.stop_event = stop_event
        self.video_queue = video_queue

    def conversion_required(self):
        # Check if conversion is enabled for at least one camera
        for camera in self.config.cameras:
            # Check also ffmpeg configuration
            camera_data = self.config.cameras[camera]
            if camera_data.video_conversion_in is None and camera_data.video_conversion_out is not None:
                logger.error(f"Missing video conversion in configuration for camera {camera}")
                return False
            elif camera_data.video_conversion_in is not None and camera_data.video_conversion_out is None:
                logger.error(f"Missing video conversion out configuration for camera {camera}")
                return False
            else:
                return True
        
        return False

    @staticmethod
    def get_temp_file(video_file):
        # Get path without extension
        index_of_sep = str(video_file).rfind('.')
        file_name = video_file[0:index_of_sep]
        extension = video_file[index_of_sep:]
        return file_name + "-tmp" + extension
    
    @staticmethod
    def get_original_backup_file(video_file):
        # Get path without extension
        index_of_sep = str(video_file).rfind('.')
        file_name = video_file[0:index_of_sep]
        extension = video_file[index_of_sep:]
        return file_name + "-original" + extension

    def convert_video(self, video_file, ffmpeg_args_in, ffmpeg_args_out):
        # Construct ffmpeg command
        logger.info(f"Converting {video_file}...")
        ffmpeg_cmd = (['nice', '-n', '19', 'ffmpeg'] + 
                ffmpeg_args_in + 
                ['-i', video_file] +
                ffmpeg_args_out + 
                [self.get_temp_file(video_file)])

        logger.debug(ffmpeg_cmd)
        # Launch process
        p = sp.run(ffmpeg_cmd, encoding='ascii', capture_output=True)
        if p.returncode != 0:
            logger.error(p.stderr)
            return False
        logger.debug(f"Conversion completed for {video_file}")
        return True
    
    def run(self):
        # Check if video conversion is required, otherwise just exit
        if not self.conversion_required:
            logger.info(f"Stopping video converter as not required")
            return

        while True:
            # Check if termination is requested
            if self.stop_event.is_set():
                logger.info(f"Exiting video converter processor...")
                break
            # Take a video to convert from the queue
            try:
                camera, video_file = self.video_queue.get(timeout=10)
            except queue.Empty:
                continue    # Check at next iteration

            # Launch ffmpeg with provided args
            camera_data = self.config.cameras[camera]

            # If requested for this camera, convert the video
            if camera_data.video_conversion_in is not None:
                logger.debug(f"Starting conversion of {video_file}")
                result = self.convert_video(video_file, camera_data.video_conversion_in, camera_data.video_conversion_out)
                # Replace original file
                if result:
                    os.replace(video_file, self.get_original_backup_file(video_file))
                    os.replace(self.get_temp_file(video_file), video_file)
            