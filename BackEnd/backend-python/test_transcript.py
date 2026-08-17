import asyncio
from youtube_transcript_api import YouTubeTranscriptApi
import json

import os
import http.cookiejar
from requests import Session

def fetch_and_print_transcript(video_id):
    try:
        print(f"\n--- Fetching transcript for Video ID: {video_id} ---")
        
        session = Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        
        cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
        if os.path.exists(cookies_path):
            cookie_jar = http.cookiejar.MozillaCookieJar(cookies_path)
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(cookie_jar)
            
        ytt_api = YouTubeTranscriptApi(http_client=session)
        transcript_list = ytt_api.list(video_id)
        
        # Iterate over all available transcripts
        for transcript in transcript_list:
            print(f"Transcript Language: {transcript.language_code}")
            
        # Get the default English transcript, or translate if needed
        try:
            transcript = transcript_list.find_transcript(['en'])
        except:
            transcript = transcript_list.find_transcript([t.language_code for t in transcript_list])
            if transcript.language_code != 'en':
                transcript = transcript.translate('en')
                
        entries = transcript.fetch()
        text_parts = []
        for e in entries:
            part = e.get("text") if isinstance(e, dict) else getattr(e, "text", "")
            text_parts.append(part)
        
        full_text = " ".join(text_parts)
        print(f"\nTranscript:\n{full_text}")
        print(f"\nTotal characters: {len(full_text)}")
        
    except Exception as e:
        print(f"Error fetching transcript for {video_id}: {e}")

if __name__ == "__main__":
    # Test with 3 sample video IDs
    sample_videos = [
        "jNQXAC9IVRw", # Me at the zoo
        "dQw4w9WgXcQ", # Rick Astley - Never Gonna Give You Up
        "M7FIvfx5J10"  # Jawed - First video on youtube? No, wait, M7FIvfx5J10 is another popular one (YouTube rewind or similar)
    ]
    
    for vid in sample_videos:
        fetch_and_print_transcript(vid)
