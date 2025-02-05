from __future__ import annotations
from io import TextIOWrapper
from multiprocessing.connection import PipeConnection
from multiprocessing.synchronize import Event
from typing import Any, Dict, List, TypeVar
import multiprocessing as mp
import ffmpeg as ff #type: ignore
import loguru
import subprocess
import smatter.utils as u
import json
import time
import pathlib

def url_into_pipe(
    stop: Event,
    _logger: loguru.Logger | None,
    base_dir: str,
    url: str,
    start: str,
    quality: str):
  """
  Uses yt-dlp and produce a stream of
  audiovideo data from a source url.
  """
  base_path = pathlib.Path(base_dir)
  yt_dlp_cache = pathlib.Path(base_dir) / 'yt-dlp-cache'
  if not base_path.exists():
    base_path.mkdir(parents=True, exist_ok=True)
  if not yt_dlp_cache.exists():
    yt_dlp_cache.mkdir(exist_ok=True)
  format = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]' if quality.isnumeric() else quality
  args = [
      'yt-dlp', url,
      '--cache-dir', "./yt-dlp-cache",
      '-o', '-', 
      '-f', format
  ]
  if start != "0":
    args.extend(["--download-sections", f'*{start}-inf'])
  yt_dlp_process = subprocess.Popen(
      args,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE if _logger else subprocess.DEVNULL,
      cwd=base_dir
  )
  log_thread = None
  if _logger:
    if not yt_dlp_process.stderr:
      _logger.warning('Could not start logging for yt-dlp process, stderr was not exposed.')
    else:
      log_thread = u.ytdl_log_messages(stop, _logger, TextIOWrapper(yt_dlp_process.stderr))
      log_thread.start()
  return yt_dlp_process, log_thread

def pipe_into_mp_queue(
    stop: Event,
    _logger: loguru.Logger,
    debug: bool,
    input_process: subprocess.Popen[bytes],
    pcm_output_queue: mp.Queue,
    passthrough_queue: mp.Queue):
  """
  Uses ffmpeg to produce a stream of
  pcm data, and also stream the input
  back to a pipe for display.
  """
  _logger.info('Starting pipe_into_mp_pipe')
  ff_in_args = {
    'nostats': None,
    'hide_banner':  None,
  }
  if debug:
    p_stderr = True
  else:
    p_stderr = False
    ff_in_args['loglevel'] = 'error' # type: ignore
  
  ff_in = ff.input('pipe:', **ff_in_args)
  ff_audio_map = ff_in.audio
  ff_out_pcm = ff.output(ff_audio_map, 'pipe:', acodec='pcm_f32le', ac=1, ar='16k', format='f32le')
  ff_process = ff.run_async(ff_out_pcm, pipe_stdin=True, pipe_stdout=True, pipe_stderr=p_stderr)
  if not input_process.stdout:
    raise Exception('Could not start yt-dlp process for {url}')
  if not ff_process.stdout or not ff_process.stdin:
    raise Exception('Could not start pcm ffmpeg process.')
  feed_thread = u.pipe_split(stop, _logger, 'pipe_split_ytdl_ffmpeg_passthrough', 8192, input_process.stdout, ff_process.stdin, passthrough_queue)
  feed_thread.start()
  pcm_feed_thread = u.pipe_to_mp_queue(stop, _logger, 'pcm_output_to_transx', 1024, ff_process.stdout, pcm_output_queue)
  pcm_feed_thread.start()
  log_thread = None
  if debug:
    if not ff_process.stderr:
      _logger.warning('Could not start logging for ffmpeg process, stderr was not exposed.')
    else:
      log_thread = u.ff_log_messages(stop, _logger, TextIOWrapper(ff_process.stderr))
      log_thread.start()

  return ff_process, feed_thread, pcm_feed_thread, log_thread


def url_into_pcm_pipe(
    stop: Event,
    _logger: loguru.Logger,
    base_dir: str,
    url: str, 
    start: str, 
    sync_pipe: PipeConnection | None):
  """
  Uses yt-dlp and ffmpeg to produce a stream of pcm data
  from a source url.
  """
  base_path = pathlib.Path(base_dir)
  yt_dlp_cache = pathlib.Path(base_dir) / 'yt-dlp-cache'
  if not base_path.exists():
    base_path.mkdir(parents=True, exist_ok=True)
  if not yt_dlp_cache.exists():
    yt_dlp_cache.mkdir(exist_ok=True)
  args = [
      'yt-dlp', url,
      '--cache-dir', "./yt-dlp-cache",
      '-S', '+size',
      '-o', '-', 
      '-f', 'ba*[acodec!*=aac]'
  ]
  if start != "0":
    args.extend(["--download-sections", f'*{start}-inf'])
  if sync_pipe:
    sync_pipe.send(time.time())
  yt_dlp_process = subprocess.Popen(
      args,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      cwd=base_dir
  )
  if not yt_dlp_process.stdout:
    raise Exception('Could not start yt-dlp process for {url}')
  _logger.info('Starting pcm ffmpeg process.')
  ff_in_args = {
    'nostats': None,
    'hide_banner':  None,
    'loglevel': 'error'
  }
  ff_in = ff.input('pipe:', **ff_in_args)
  ff_audio_map = ff_in.audio
  ff_out_pcm = ff.output(ff_audio_map, 'pipe:', acodec='pcm_f32le', ac=1, ar='16k', format='f32le')
  ff_process = ff.run_async(ff_out_pcm, pipe_stdout=True, pipe_stdin=True)
  if not ff_process.stdout or not ff_process.stdin:
    raise Exception('Could not start pcm ffmpeg process.')
  feed_thread = u.pipe_to_pipe(stop, _logger, 'ytdlp_to_ffmepg_pcm', 8192, yt_dlp_process.stdout, ff_process.stdin)
  feed_thread.start()
  return yt_dlp_process, ff_process, feed_thread

def probe(_logger: loguru.Logger, url: str):
  """
  Uses yt-dlp to probe the source url for information
  """
  _logger.info('Starting yt-dlp process.')
  args = [
      "yt-dlp", url,
      "-j"
  ]
  yt_dlp_process = subprocess.Popen(
      args,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL
  )
  out, _err = yt_dlp_process.communicate(None, None)

  return json.decoder.JSONDecoder().decode(out.decode('utf-8'))