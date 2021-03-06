"""
    pyIDM

    multi-connections internet download manager, based on "pyCuRL/curl", "youtube_dl", and "PySimpleGUI"

    :copyright: (c) 2019-2020 by Mahmoud Elshahat.
    :license: GNU LGPLv3, see LICENSE for more details.
"""
import copy
import os
import re
import zipfile
import time
from urllib.parse import urljoin

from . import config
from .downloaditem import DownloadItem, Segment
from .utils import log, validate_file_name, get_headers, size_format, run_command, size_splitter, get_seg_size, \
    delete_file, download, process_thumbnail

# youtube-dl
ytdl = None  # youtube-dl will be imported in a separate thread to save loading time


class Logger(object):
    """used for capturing youtube-dl stdout/stderr output"""

    def debug(self, msg):
        log(msg)

    def error(self, msg):
        log(msg)

    def warning(self, msg):
        log(msg)

    def __repr__(self):
        return "youtube-dl Logger"


def get_ytdl_options():
    ydl_opts = {'ignoreerrors': True, 'prefer_insecure': False, 'no_warnings': False, 'logger': Logger()}
    if config.proxy:
        ydl_opts['proxy'] = config.proxy

    # set Referer website
    if config.referer_url:
        # this is not accessible via youtube-dl options, changing standard headers is the only way
        ytdl.utils.std_headers['Referer'] = config.referer_url

    # website authentication
    if config.username or config.password:
        ydl_opts['username'] = config.username
        ydl_opts['password'] = config.password

    # subtitle
    # ydl_opts['listsubtitles'] = True  # this is has a problem with playlist
    # ydl_opts['allsubtitles'] = True  # has no effect
    ydl_opts['writesubtitles'] = True
    ydl_opts['writeautomaticsub'] = True


    # if config.log_level >= 3:
        # ydl_opts['verbose'] = True  # it make problem with Frozen PyIDM, extractor doesn't work
    # elif config.log_level <= 1:
    #     ydl_opts['quiet'] = True  # it doesn't work

    return ydl_opts


class Video(DownloadItem):
    """represent a youtube video object, interface for youtube-dl"""

    def __init__(self, url, vid_info=None):
        super().__init__(folder=config.download_folder)
        self.url = url
        self.resumable = True
        self.vid_info = vid_info  # a youtube-dl dictionary contains video information

        # let youtube-dl fetch video info
        if self.vid_info is None:
            with ytdl.YoutubeDL(get_ytdl_options()) as ydl:
                self.vid_info = ydl.extract_info(self.url, download=False, process=True)

        self.webpage_url = url  # self.vid_info.get('webpage_url')
        self.title = validate_file_name(self.vid_info.get('title', f'video{int(time.time())}'))
        self.name = self.title

        # streams
        self.stream_names = []  # names in a list
        self.raw_stream_names = []  # names but without size
        self.stream_list = []  # streams in a list
        self.video_streams = {}
        self.mp4_videos = {}
        self.other_videos = {}
        self.audio_streams = {}
        self._streams = {}
        self.raw_streams = {}

        self.stream_menu = []  # it will be shown in video quality combo box != self.stream.names
        self.raw_stream_menu = []  # same as self.stream_menu but without size
        self._selected_stream = None

        # thumbnail
        self.thumbnail_url = ''

        # flag for processing raw video info by youtube-dl
        self.processed = False

        self.setup()

    def __repr__(self):
        return f'Video object( name: {self.name}, url:{self.url}'

    def setup(self):
        url = self.vid_info.get('url', None) or self.vid_info.get('webpage_url', None) or self.vid_info.get('id', None)
        if url:
            self.url = url

        self.webpage_url = url  # self.vid_info.get('webpage_url')
        self.name = self.title = validate_file_name(self.vid_info.get('title', f'video{int(time.time())}'))

        # thumbnail
        self.thumbnail_url = self.vid_info.get('thumbnail', '')

        # subtitles
        self.subtitles = self.vid_info.get('subtitles', {})
        self.automatic_captions = self.vid_info.get('automatic_captions', {})

        # build streams
        self._process_streams()

    def _process_streams(self):
        """ Create Stream object lists"""
        all_streams = [Stream(x) for x in self.vid_info['formats']]
        all_streams.reverse()  # get higher quality first

        # prepare some categories
        normal_streams = {stream.raw_name: stream for stream in all_streams if stream.mediatype == 'normal'}
        dash_streams = {stream.raw_name: stream for stream in all_streams if stream.mediatype == 'dash'}

        # normal streams will overwrite same streams names in dash
        video_streams = {**dash_streams, **normal_streams}

        # sort streams based on quality, "youtube-dl will provide a sorted list, this step is not necessary"
        video_streams = {k: v for k, v in sorted(video_streams.items(), key=lambda item: item[1].quality, reverse=True)}

        # sort based on mp4 streams first
        mp4_videos = {stream.name: stream for stream in video_streams.values() if stream.extension == 'mp4'}
        other_videos = {stream.name: stream for stream in video_streams.values() if stream.extension != 'mp4'}
        video_streams = {**mp4_videos, **other_videos}

        audio_streams = {stream.name: stream for stream in all_streams if stream.mediatype == 'audio'}

        # add another audio formats, mp3, aac, wav, ogg
        if audio_streams:
            audio = list(audio_streams.values())
            webm = [stream for stream in audio if stream.extension == 'webm']
            m4a = [stream for stream in audio if stream.extension in ('m4a')]

            aac = m4a[0] if m4a else audio[0]
            aac = copy.copy(aac)
            aac.extension = 'aac'

            ogg = webm[0] if webm else audio[0]
            ogg = copy.copy(ogg)
            ogg.extension = 'ogg'

            mp3 = copy.copy(aac)
            mp3.extension = 'mp3'
            mp3.abr = 128

            extra_audio = {aac.name: aac, ogg.name: ogg, mp3.name: mp3}
            extra_audio.update(**audio_streams)
            audio_streams = extra_audio

        # collect all in one dictionary of stream.name: stream pairs
        streams = {**video_streams, **audio_streams}

        # get extra streams
        extra_streams = {stream.name: stream for stream in all_streams if stream not in streams.values()}

        # update streams
        streams.update(**extra_streams)

        stream_menu = ['● Video streams:                     '] + list(mp4_videos.keys()) + list(other_videos.keys()) \
                    + ['', '● Audio streams:                 '] + list(audio_streams.keys()) \
                    + ['', '● Extra streams:                 '] + list(extra_streams.keys()) \

        # assign variables
        self.stream_list = list(streams.values())
        self.stream_names = [stream.name for stream in self.stream_list]
        self.raw_stream_names = [stream.raw_name for stream in self.stream_list]
        self.video_streams = video_streams
        self.mp4_videos = mp4_videos
        self.other_videos = other_videos
        self.audio_streams = audio_streams

        self._streams = streams
        self.raw_streams = {stream.raw_name: stream for stream in streams.values()}
        self.stream_menu = stream_menu
        self.raw_stream_menu = [x.rsplit(' -', 1)[0] for x in stream_menu]

    @property
    def streams(self):
        """ Returns dictionary of all streams sorted  key=stream.name, value=stream object"""
        if not self._streams:
            self._process_streams()

        return self._streams

    @property
    def selected_stream_index(self):
        return self.stream_list.index(self.selected_stream)

    @property
    def selected_stream(self):
        if not self._selected_stream:
            self._selected_stream = self.stream_list[0]  # select first stream

        return self._selected_stream

    @selected_stream.setter
    def selected_stream(self, stream):
        if type(stream) is not Stream:
            raise TypeError('value must be a Stream object')

        self._selected_stream = stream
        self.selected_quality = stream.raw_name

        self.update_param()

    def get_thumbnail(self):
        if self.thumbnail_url and not self.thumbnail:
            self.thumbnail = process_thumbnail(self.thumbnail_url)

    def update_param(self, audio_stream=None):
        """Mainly used when select a stream for current video object"""
        # do some parameters updates
        stream = self.selected_stream
        self.name = self.title + '.' + stream.extension
        self.eff_url = stream.url
        self.size = stream.size
        self.fragment_base_url = stream.fragment_base_url
        self.fragments = stream.fragments
        self.protocol = stream.protocol
        self.format_id = stream.format_id
        self.manifest_url = stream.manifest_url
        self.width = stream.width
        self.height = stream.height
        self.abr = stream.abr
        self.tbr = stream.tbr

        # set type ---------------------------------------------------------------------------------------
        self.type = stream.mediatype if stream.mediatype == 'audio' else 'video'

        # set subtype
        self.subtype_list.clear()

        if stream.mediatype in ('dash', 'normal'):
            self.subtype_list.append(stream.mediatype)

        if 'm3u8' in self.protocol:
            self.subtype_list.append('hls')

        if self.fragments:
            self.subtype_list.append('fragmented')

        # select an audio to embed if our stream is dash video
        audio_streams_list = [stream for stream in self.stream_list if stream.mediatype == 'audio']  # audio streams in a list

        # sort audio list
        audio_streams_list = sorted(audio_streams_list, key=lambda stream: stream.quality, reverse=True)

        if stream.mediatype == 'dash' and audio_streams_list:
            # auto select audio stream if no parameter given
            if not audio_stream:
                matching_stream = [audio for audio in audio_streams_list if audio.extension == stream.extension
                            or (audio.extension == 'm4a' and stream.extension == 'mp4')]
                # if failed to find a matching audio, choose any one
                if matching_stream:
                    audio_stream = matching_stream[0]
                else:
                    audio_stream = audio_streams_list[0]

            self.audio_stream = audio_stream
            self.audio_url = audio_stream.url
            self.audio_size = audio_stream.size
            self.audio_fragment_base_url = audio_stream.fragment_base_url
            self.audio_fragments = audio_stream.fragments
            self.audio_format_id = audio_stream.format_id
        else:
            self.audio_url = None
            self.audio_fragment_base_url = None
            self.audio_fragments = None
            self.audio_format_id = None

    def refresh(self):
        """will be used in case we updated vid_info dictionary from youtube-dl"""
        # reset properties and rebuild streams
        self.setup()


def process_video_info(vid, getthumbnail=True):
    try:
        with ytdl.YoutubeDL(get_ytdl_options()) as ydl:
            vid_info = ydl.process_ie_result(vid.vid_info, download=False)
            if vid_info:
                vid.vid_info = vid_info
                vid.refresh()

            if vid and getthumbnail:
                vid.get_thumbnail()

        log('process_video_info()> processed url:', vid.url, log_level=3)
        vid.processed = True
    except Exception as e:
        log('process_video_info()> error:', e)


class Stream:
    def __init__(self, stream_info):
        # fetch data from youtube-dl stream_info dictionary
        self.format_id = stream_info.get('format_id', '')
        self.url = stream_info.get('url', None)
        self.player_url = stream_info.get('player_url', None)
        self.extension = stream_info.get('ext', None)
        self.width = stream_info.get('width', 0)
        self.height = stream_info.get('height', 0)
        self.fps = stream_info.get('fps', None)
        self.format_note = stream_info.get('format_note', '')
        self.acodec = stream_info.get('acodec', None)
        self.abr = stream_info.get('abr', 0)
        self.tbr = stream_info.get('tbr', 0)
        self.size = stream_info.get('filesize', None)
        # self.quality = stream_info.get('quality', None)
        self.vcodec = stream_info.get('vcodec', None)
        self.res = stream_info.get('resolution', None)
        self.downloader_options = stream_info.get('downloader_options', None)
        self.format = stream_info.get('format', None)
        self.container = stream_info.get('container', None)

        # protocol
        self.protocol = stream_info.get('protocol', '')

        # calculate some values
        self.rawbitrate = stream_info.get('abr', 0) * 1024
        self._mediatype = None
        self.resolution = f'{self.width}x{self.height}' if (self.width and self.height) else ''

        # fragmented video streams
        self.fragment_base_url = stream_info.get('fragment_base_url', None)
        self.fragments = stream_info.get('fragments', None)

        # get missing size
        if self.fragments or 'm3u8' in self.protocol:
            # ignore fragmented streams, since the size coming from headers is for first fragment not whole file
            self.size = 0
        if not isinstance(self.size, int):
            self.size = self.get_size()

        # hls stream specific
        self.manifest_url = stream_info.get('manifest_url', '')

        # print(self.name, self.size, isinstance(self.size, int))

    def get_size(self):
        headers = get_headers(self.url)
        size = int(headers.get('content-length', 0))
        print('stream.get_size()>', self.name)
        return size

    @property
    def name(self):
        return f'      ›  {self.extension} - {self.quality} - {size_format(self.size)}'  # ¤ » ›

    @property
    def raw_name(self):
        return f'      ›  {self.extension} - {self.quality}'

    @property
    def quality(self):
        try:
            if self.mediatype == 'audio':
                return int(self.abr)
            else:
                return int(self.height)
        except:
            return 0

    def __repr__(self, include_size=True):
        return self.name

    @property
    def mediatype(self):
        if not self._mediatype:
            if self.vcodec == 'none':
                self._mediatype = 'audio'
            elif self.acodec == 'none':
                self._mediatype = 'dash'
            else:
                self._mediatype = 'normal'

        return self._mediatype


def download_ffmpeg(destination=config.sett_folder):
    """it should download ffmpeg.exe for windows os"""

    # set download folder
    config.ffmpeg_download_folder = destination

    # first check windows 32 or 64
    import platform
    # ends with 86 for 32 bit and 64 for 64 bit i.e. Win7-64: AMD64 and Vista-32: x86
    if platform.machine().endswith('64'):
        # 64 bit link
        url = 'https://github.com/pyIDM/pyIDM/releases/download/extra/ffmpeg.zip'
    else:
        # 32 bit link
        url = 'https://github.com/pyIDM/pyIDM/releases/download/extra/ffmpeg_32bit.zip'

    log('downloading: ', url)

    # create a download object, will store ffmpeg in setting folder
    # print('config.sett_folder = ', config.sett_folder)
    d = DownloadItem(url=url, folder=config.ffmpeg_download_folder)
    d.update(url)
    d.name = 'ffmpeg.zip'  # must rename it for unzip to find it
    # print('d.folder = ', d.folder)

    # post download
    d.callback = 'unzip_ffmpeg'

    # send download request to main window
    config.main_window_q.put(('download', (d, False)))


def unzip_ffmpeg():
    log('unzip_ffmpeg:', 'unzipping')

    try:
        file_name = os.path.join(config.ffmpeg_download_folder, 'ffmpeg.zip')
        with zipfile.ZipFile(file_name, 'r') as zip_ref:  # extract zip file
            zip_ref.extractall(config.ffmpeg_download_folder)

        log('ffmpeg update:', 'delete zip file')
        delete_file(file_name)
        log('ffmpeg update:', 'ffmpeg .. is ready at: ', config.ffmpeg_download_folder)
    except Exception as e:
        log('unzip_ffmpeg: error ', e)


def check_ffmpeg():
    """check for ffmpeg availability, first: current folder, second config.global_sett_folder,
    and finally: system wide"""

    log('check ffmpeg availability?')
    found = False

    # search in current app directory then default setting folder
    try:
        for folder in [config.current_directory, config.global_sett_folder]:
            for file in os.listdir(folder):
                # print(file)
                if file == 'ffmpeg.exe':
                    found = True
                    config.ffmpeg_actual_path = os.path.join(folder, file)
                    break
            if found:  # break outer loop
                break
    except:
        pass

    # Search in the system
    if not found:
        cmd = 'where ffmpeg' if config.operating_system == 'Windows' else 'which ffmpeg'
        error, output = run_command(cmd, verbose=False)
        if not error:
            found = True

            # fix issue 47 where command line return \n\r with path
            output = output.strip()
            config.ffmpeg_actual_path = os.path.realpath(output)

    if found:
        log('ffmpeg checked ok! - at: ', config.ffmpeg_actual_path)
        return True
    else:
        log(f'can not find ffmpeg!!, install it, or add executable location to PATH, or copy executable to ',
            config.global_sett_folder, 'or', config.current_directory)


def merge_video_audio(video, audio, output, d):
    """merge video file and audio file into output file, d is a reference for current DownloadItem object"""
    log('merging video and audio')

    # ffmpeg file full location
    ffmpeg = config.ffmpeg_actual_path

    # very fast audio just copied, format must match [mp4, m4a] and [webm, webm]
    cmd1 = f'"{ffmpeg}" -y -i "{video}" -i "{audio}" -c copy "{output}"'

    # slow, mix different formats
    cmd2 = f'"{ffmpeg}" -y -i "{video}" -i "{audio}" "{output}"'

    verbose = True if config.log_level >= 2 else False

    # run command with shell=False if failed will use shell=True option
    error, output = run_command(cmd1, verbose=verbose, shell=True, d=d)

    # retry on error with cmd2
    if error:
        error, output = run_command(cmd2, verbose=verbose, shell=True, d=d)

    return error, output
            

def import_ytdl():
    # import youtube_dl using thread because it takes sometimes 20 seconds to get imported and impact app startup time
    start = time.time()
    global ytdl, ytdl_version
    try:
        import youtube_dl as ytdl

        # update version value
        config.ytdl_VERSION = ytdl.version.__version__

        # get a random user agent and update headers
        config.HEADERS['User-Agent'] = ytdl.utils.random_user_agent()

        # calculate loading time
        load_time = time.time() - start
        log(f'youtube-dl load_time= {int(load_time)} seconds')
    except Exception as e:
        log('import_ytdl()> error', e)


def pre_process_hls(d):
    """
    handle m3u8 manifest file and build a local m3u8 file and download item segments
    :param d: DownloadItem() object
    """

    log('pre_process_hls()> start processing', d.name)

    # create temp_folder if doesn't exist
    if not os.path.isdir(d.temp_folder):
        try:
            os.makedirs(d.temp_folder)
        except:
            return False

    # download m3u8 files ----------------------------------------------------------------------------------------
    def download_m3u8(url):
        # download the manifest from m3u8 file descriptor located at url
        buffer = download(url)  # get BytesIO object

        if buffer:
            # convert to string
            buffer = buffer.getvalue().decode()

            # verify file is m3u8 format
            if '#EXT' in repr(buffer):
                return buffer

        log('pre_process_hls()> received invalid m3u8 file from server')
        if config.log_level >= 3:
            log('---------------------------------------\n', buffer, '---------------------------------------\n')
        return None

    # get correct url of m3u8 file from playlist or master m3u8 manifest, youtube-dl sometimes gives wrong url
    def get_correct_m3u8_url(master_m3u8_doc, media='video'):
        if not master_m3u8_doc:
            return False

        lines = master_m3u8_doc.splitlines()
        for i, line in enumerate(lines):

            # video_checks = any([str(x) in line for x in (d.width, d.height, d.format_id) if x is not None])
            # audio_checks = any([str(x) in line for x in (d.abr, d.tbr, d.audio_format_id) if x is not None])

            # todo: these checks are very fragile, we need to parse line into dict
            video_checks = (str(d.width) in line and str(d.height) in line or d.format_id in line)
            audio_checks = (str(d.abr) in line or str(d.tbr) in line or d.audio_format_id in line)

            found = video_checks if media == 'video' else audio_checks

            if found:
                # url maybe in same line with "URI" notation or at the next line
                match = re.search(r'URI="(.*)"', line)
                if match:
                    url = match.group(1)
                elif not lines[i + 1].startswith('#'):
                    url = lines[i + 1]
                else:
                    return False

                correct_url = urljoin(d.manifest_url, url)
                return correct_url

    master_m3u8 = download_m3u8(d.manifest_url)
    video_m3u8 = download_m3u8(d.eff_url)
    audio_m3u8 = download_m3u8(d.audio_url)

    if not video_m3u8:
        eff_url = get_correct_m3u8_url(master_m3u8, media='video')
        if not eff_url:
            log('pre_process_hls()> Failed to get correct video m3u8 url, quitting!')
            return False
        else:
            d.eff_url = eff_url
            video_m3u8 = download_m3u8(d.eff_url)

    if 'dash' in d.subtype_list and not audio_m3u8:
        eff_url = get_correct_m3u8_url(master_m3u8, media='audio')
        if not eff_url:
            log('pre_process_hls()> Failed to get correct audio m3u8 url, quitting!')
            return False
        else:
            d.audio_url = eff_url
            audio_m3u8 = download_m3u8(d.audio_url)

    # save remote m3u8 files to disk
    with open(os.path.join(d.temp_folder, 'remote_video.m3u8'), 'w') as f:
        f.write(video_m3u8)

    if 'dash' in d.subtype_list:
        with open(os.path.join(d.temp_folder, 'remote_audio.m3u8'), 'w') as f:
            f.write(audio_m3u8)

    # ---------------------------------------------------------------------------------------------------------

    # process remote m3u8 files -------------------------------------------------------------------------------
    def process_m3u8(file, type_='video'):
        """
        process m3u8 file, extract urls, build local m3u8 file, and build segments for download item
        :param file: m3u8 as a file object
        :param type_: 'video' or 'audio'
        :return: None
        """

        base_url = d.eff_url if type_=='video' else d.audio_url
        seg_name = 'v' if type_ == 'video' else 'a'

        url_list = []
        local_lines = []
        local_lines2 = []
        lines = file.splitlines()

        # iterate over all m3u8 file lines
        for i, line in enumerate(lines[:]):
            url = ''
            line2 = line

            # lines doesn't start with # is a media links
            if line and not line.startswith('#'):
                # get absolute url from relative paths
                url = urljoin(base_url, line)
                line2 = url

                # build line for local m3u8 file with reference to local segment file
                line = os.path.join(d.temp_folder, f'{seg_name}{i}')

            # handle buried urls inside lines ex: # '#EXT-X-KEY:METHOD=AES-128,URI="https://content-aus...62a9",IV=0x0000'
            elif line.startswith('#'):
                match = re.search(r'URI="(.*)"', line)
                if match:
                    url = match.group(1)
                    # get absolute url from relative paths
                    url = urljoin(base_url, url)

                    line2 = line.replace(match.group(1), url)
                    line = line.replace(match.group(1), os.path.join(d.temp_folder, f'{seg_name}{i}'))

            # process line and convert '\' to '/'
            line = line.replace('\\', '/')
            line2 = line2.replace('\\', '/')

            local_lines2.append(line2)
            local_lines.append(line)

            url_list.append(url)

        # write m3u8 file with absolute paths for debugging
        name = 'remote_video2.m3u8' if type_ == 'video' else 'remote_audio2.m3u8'
        local_file = os.path.join(d.temp_folder, name)
        with open(os.path.join(d.temp_folder, local_file), 'w') as f:
            f.write('\n'.join(local_lines2))

        # write local m3u8 file
        name = 'local_video.m3u8' if type_ == 'video' else 'local_audio.m3u8'
        local_file = os.path.join(d.temp_folder, name)
        with open(os.path.join(d.temp_folder, local_file), 'w') as f:
            f.write('\n'.join(local_lines))

        # create segments
        seg_name = 'v' if type_ == 'video' else 'a'
        d._segments += [Segment(name=os.path.join(d.temp_folder, f'{seg_name}{i}'), num=i, range=None, size=0,
                                url=seg_url, tempfile=d.temp_file, merge=False)
                        for i, seg_url in enumerate(url_list) if seg_url]

    # reset segments
    d._segments = []

    # send video m3u8 file for processing
    process_m3u8(video_m3u8, type_='video')

    # send audio m3u8 file for processing
    if 'dash' in d.subtype_list:
        process_m3u8(audio_m3u8, type_='audio')

    # load previous segment information from disk - resume download -
    d.load_progress_info()

    log('pre_process_hls()> done processing', d.name)

    return True


def post_process_hls(d):
    """ffmpeg will process m3u8 files"""

    log('post_process_hls()> start processing', d.name)

    local_video_m3u8_file = os.path.join(d.temp_folder, 'local_video.m3u8')
    local_audio_m3u8_file = os.path.join(d.temp_folder, 'local_audio.m3u8')

    cmd = f'"{config.ffmpeg_actual_path}" -y -protocol_whitelist "file,http,https,tcp,tls,crypto"  ' \
          f'-allowed_extensions ALL -i "{local_video_m3u8_file}" -c copy -f mp4 "file:{d.temp_file}"'

    error, output = run_command(cmd, d=d)
    if error:
        log('post_process_hls()> ffmpeg failed:', output)
        return False

    if 'dash' in d.subtype_list:
        cmd = f'"{config.ffmpeg_actual_path}" -y -protocol_whitelist "file,http,https,tcp,tls,crypto"  ' \
              f'-allowed_extensions ALL -i "{local_audio_m3u8_file}" -c copy -f mp4 "file:{d.audio_file}"'

        error, output = run_command(cmd, d=d)
        if error:
            log('post_process_hls()> ffmpeg failed:', output)
            return False

    log('post_process_hls()> done processing', d.name)

    return True


def convert_audio(d):
    """
    convert audio formats
    :param d: DownloadItem object
    :return: bool True for success or False when failed
    """
    # famous formats: mp3, aac, wav, ogg
    infile = d.temp_file
    outfile = d.target_file

    # look for compatible formats and use "copy" parameter for faster processing
    cmd1 = f'ffmpeg -y -i "{infile}" -acodec copy "{outfile}"'

    # general command, consume time
    cmd2 = f'ffmpeg -y -i "{infile}" "{outfile}"'

    # run command1
    error, _ = run_command(cmd1, verbose=True, shell=True)

    if error:
        error, _ = run_command(cmd2, verbose=True, shell=True)

    if error:
        return False
    else:
        return True





















