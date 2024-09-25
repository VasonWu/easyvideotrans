import os
import json
import requests
import zipfile
import shutil
import uuid
from src.service.audio_processing.audio_remove import audio_remove
from src.service.audio_processing.transcribe_audio import transcribe_audio_en
from src.service.audio_processing.voice_connect import connect_voice
from src.service.translation import get_translator, srt_sentense_merge
from src.service.tts import get_tts_client
from src.task_manager.celery_tasks.tasks import video_preview_task
from src.task_manager.celery_tasks.celery_utils import get_queue_length
from werkzeug.utils import secure_filename
from pytubefix import YouTube
from moviepy.editor import VideoFileClip
from functools import wraps
from flask import Flask, request, jsonify, render_template, send_from_directory
from prometheus_flask_exporter import PrometheusMetrics

import logging

# 读取配置文件
logger = logging.getLogger('my_app')
def load_config():
    config_path = 'configs/pytvzhen.json'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config

# 批量处理视频文件
def batch():
    config = load_config()  # 加载配置
    output_path = config['OUTPUT_PATH']

    video_files = []
    for root, _, files in os.walk(os.path.join(output_path, 'videos')):
        for file in files:
            if file.endswith(('.mp4', '.avi', '.mkv', '.mov')):
                video_files.append(os.path.join(root, file))

    for video_path in video_files:
        process_video(video_path, config)

# 处理单个视频文件
def process_video(video_path, config):
    # 获取视频文件所在的目录
    video_dir = os.path.dirname(video_path)

    # 1. 提取音频
    extra_audio(video_path, video_dir)

    # 2. 去除音频背景
    remove_audio_bg(video_path, video_dir, config)

    # 3. 转录音频
    transcribe(video_path, video_dir)

    # 4. 翻译成中文
    translate_to_zh(video_path, video_dir)

    # 5. 文本转语音
    tts(video_path, video_dir)

    # 6. 连接语音
    voice_connect(video_path, video_dir)

    # 7. 生成视频预览
    video_preview(video_path, video_dir)

# 提取音频
def extra_audio(video_path, video_dir):
    video_name = os.path.basename(video_path)
    audio_fn = f'{os.path.splitext(video_name)[0]}.wav'
    audio_path = os.path.join(video_dir, audio_fn)

    if os.path.exists(audio_path):
        print(f"音频已经存在 {audio_path}, 跳过提取.")
    else:
        with VideoFileClip(video_path) as video:
            video.audio.write_audiofile(audio_path)
        print(f"从 {video_path} 提取音频到 {audio_path}.")

def remove_audio_bg(video_path, video_dir, config):
    video_name = os.path.basename(video_path)
    audio_fn = f'{os.path.splitext(video_name)[0]}.wav'
    audio_bg_fn = f'{os.path.splitext(video_name)[0]}_bg.wav'
    audio_no_bg_fn = f'{os.path.splitext(video_name)[0]}_no_bg.wav'

    audio_path = os.path.join(video_dir, audio_fn)
    audio_bg_path = os.path.join(video_dir, audio_bg_fn)
    audio_no_bg_path = os.path.join(video_dir, audio_no_bg_fn)

    if os.path.exists(audio_no_bg_path):
        print(f"无背景音频已经存在 {audio_no_bg_path}, 跳过.")
    else:
        # 从配置文件中获取必要的参数
        instrument_file_path = config['REMOVE_BACKGROUND_MUSIC_BASELINE_MODEL_PATH']
        pytorch_device = config['REMOVE_BACKGROUND_MUSIC_TORCH_DEVICE']

        # 调用 audio_remove 方法，传入音频路径和模型路径
        audio_remove(audio_path, audio_no_bg_path, audio_bg_path, instrument_file_path, pytorch_device)
        print(f"已为 {audio_path} 去除背景音，输出为 {audio_no_bg_path}.")


# 转录音频
def transcribe(video_path, video_dir):
    video_name = os.path.basename(video_path)
    audio_no_bg_fn = f'{os.path.splitext(video_name)[0]}_no_bg.wav'
    audio_no_bg_path = os.path.join(video_dir, audio_no_bg_fn)
    en_srt_fn = f'{os.path.splitext(video_name)[0]}_en.srt'
    en_srt_path = os.path.join(video_dir, en_srt_fn)
    en_srt_merged_fn = f'{os.path.splitext(video_name)[0]}_en_merged.srt'
    en_srt_merged_path = os.path.join(video_dir, en_srt_merged_fn)

    language = 'en'  # 假设语言为英文
    transcribe_model = 'medium'

    print('111')
    transcribe_audio_en(logger, path=audio_no_bg_path, modelName=transcribe_model, language=language, srtFilePathAndName=en_srt_path)
    print('222')
    srt_sentense_merge(logger, en_srt_path, en_srt_merged_path)
    print('333')
    


# 翻译转录内容为中文
def translate_to_zh(video_path, video_dir):
    video_name = os.path.basename(video_path)
    video_dir = os.path.dirname(video_path)
    
    # 定义文件路径
    en_srt_merged_fn = f'{os.path.splitext(video_name)[0]}_en_merged.srt'
    zh_srt_merged_fn = f'{os.path.splitext(video_name)[0]}_zh_merged.srt'
    en_srt_merged_path = os.path.join(video_dir, en_srt_merged_fn)
    zh_srt_merged_path = os.path.join(video_dir, zh_srt_merged_fn)

    # 检查英文字幕文件是否存在
    if not os.path.exists(en_srt_merged_path):
        print(f'Warning: English SRT {en_srt_merged_fn} not found at {en_srt_merged_path}.')
        return

    # 检查支持的翻译厂商
    translate_vendor = "google"
    api_key = ""

    try:
        # 获取翻译器实例
        #translator = get_translator(translate_vendor, api_key, proxies=None)
        translator = get_translator(translate_vendor, api_key, proxies='http://192.168.150.181:1087')

        # 调用翻译器进行字幕翻译
        ret = translator.translate_srt(source_file_name_and_path=en_srt_merged_path,
                                       output_file_name_and_path=zh_srt_merged_path)

        if ret:
            print(f"Info: Using {translate_vendor} to translate SRT from {en_srt_merged_fn} to {zh_srt_merged_fn} successfully.")
        else:
            print(f"Warning: {translate_vendor} translation failed.")

    except ValueError as e:
        print(f"Error: {str(e)}")

# 文本转语音
def tts(video_path, video_dir):
    video_name = os.path.basename(video_path)
    srt_path = os.path.join(video_dir, f'{os.path.splitext(video_name)[0]}_zh_merged.srt')
    tts_dir = os.path.join(video_dir, f'{os.path.splitext(video_name)[0]}_zh_source')
    #character = "zh-CN-XiaoyiNeural" #female
    character = "zh-CN-YunjianNeural" #male
    
    if os.path.exists(tts_dir):
        # delete old tts dir
        shutil.rmtree(tts_dir)

    tts_client = get_tts_client("edge", character)
    tts_client.srt_to_voice(srt_path, tts_dir)

    print("tts finished")

# 连接语音到视频
def voice_connect(video_path, video_dir):
    video_name = os.path.basename(video_path)
    voice_dir = os.path.join(video_dir, f'{os.path.splitext(video_name)[0]}_zh_source')

    voice_connect_fn = f'{os.path.splitext(video_name)[0]}_zh.wav'
    voice_connect_path = os.path.join(video_dir, voice_connect_fn)
    warning_log_fn = f'{os.path.splitext(video_name)[0]}_connect_warning.log'
    warning_log_path = os.path.join(video_dir, warning_log_fn)

    if not os.path.exists(voice_dir):
        return jsonify({"message": log_warning_return_str(
            f'Voice directory {voiceDir} not found at {output_path}')}), 404

    ret = connect_voice(logger, voice_dir, voice_connect_path, warning_log_path)
    print(f"语音已连接到视频，输出视频为 {voice_connect_path}.")

# 生成视频预览
def video_preview(video_path, video_dir):
    video_name = os.path.basename(video_path)
    voice_dir = os.path.join(video_dir, f'{os.path.splitext(video_name)[0]}_zh_source')
    
    voice_connect_path = os.path.join(video_dir, f'{os.path.splitext(video_name)[0]}_zh.wav')
    audio_bg_path = os.path.join(video_dir, f'{os.path.splitext(video_name)[0]}_bg.wav')
    video_save_path = os.path.join(video_dir, video_name)
    video_fhd_save_path = os.path.join(video_dir, f"{os.path.splitext(video_name)[0]}_fhd.mp4")
    video_out_path = os.path.join(video_dir, f"{os.path.splitext(video_name)[0]}_preview.mp4")

    if not os.path.exists(voice_connect_path) or not os.path.exists(audio_bg_path):
        print(f'Warning: Chinese Voice {os.path.basename(voice_connect_path)} not found in {voice_connect_path}')
        return

    if not os.path.exists(video_save_path) and not os.path.exists(video_fhd_save_path):
        print("Warning: No video found")
        return

    video_source_path = video_fhd_save_path if os.path.exists(video_fhd_save_path) else video_save_path

    print(f"Starting video preview generation for {video_name}...")
    print(f"Using video source: {video_source_path}")
    print(f"Voice connection path: {voice_connect_path}")
    print(f"Background audio path: {audio_bg_path}")
    print(f"Output preview path: {video_out_path}")

    # 这里可以添加实际的视频预览生成逻辑
    video_preview_task(video_source_path, voice_connect_path, audio_bg_path, video_out_path)

    print(f"Video preview for {video_name} successfully rendered.")


if __name__ == "__main__":
    batch()
   # video_preview("a.mp4", "/app/output/videos/")

