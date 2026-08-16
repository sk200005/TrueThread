import sys
from youtube_transcript_api import YouTubeTranscriptApi
import http.cookiejar
from requests import Session
import os

video_id = "80yIVH2aOy0" # The one that got IP blocked

session = Session()
try:
    if os.path.exists('cookies.txt'):
        cookie_jar = http.cookiejar.MozillaCookieJar('cookies.txt')
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(cookie_jar)
        print("Cookies loaded!")
    else:
        print("cookies.txt not found!")
except Exception as e:
    print("Cookie load error:", e)

ytt_api = YouTubeTranscriptApi(http_client=session)
try:
    transcript_list = ytt_api.list(video_id)
    transcript = transcript_list.find_transcript(['en'])
    print(transcript.fetch()[:2])
except Exception as e:
    print("Failed:", e)
