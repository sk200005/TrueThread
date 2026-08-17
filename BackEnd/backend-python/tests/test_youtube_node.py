import pytest

from app.graphs.nodes import youtube_node


def _video(video_id: str) -> dict:
    return {
        "videoId": video_id,
        "title": f"Video {video_id}",
        "channel": "Test Channel",
        "publishedAt": "2026-08-17T00:00:00Z",
        "views": 100,
        "likes": 10,
        "duration": "1:00",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "description": "Test description",
        "thumbnail_url": "https://example.com/thumb.jpg",
    }


@pytest.mark.asyncio
async def test_youtube_fetch_continues_past_candidates_without_transcripts(monkeypatch):
    video_ids = [f"v{i}" for i in range(12)]

    async def fake_search_videos(query, max_results):
        assert max_results == youtube_node.SEARCH_CANDIDATE_COUNT
        return video_ids

    async def fake_get_video_metadata(ids, target_count):
        assert ids == video_ids
        return [_video(video_id) for video_id in ids]

    def fake_get_transcript_with_reason(video_id):
        index = video_ids.index(video_id)
        if index < 7:
            return None, "no caption track available"
        return {"text": f"Transcript for {video_id}", "lang": "en", "original_lang": "en"}, None

    monkeypatch.setattr(youtube_node.youtube_client, "search_videos", fake_search_videos)
    monkeypatch.setattr(youtube_node.youtube_client, "get_video_metadata", fake_get_video_metadata)
    monkeypatch.setattr(
        youtube_node.youtube_client,
        "get_transcript_with_reason",
        fake_get_transcript_with_reason,
    )

    result = await youtube_node.youtube_fetch({"query": "test query", "sources": {}})
    youtube_result = result["sources"]["youtube"]

    assert youtube_result["status"] == "done"
    assert len(youtube_result["documents"]) == youtube_node.TARGET_COUNT
    assert len(youtube_result["skipped"]) == 7


@pytest.mark.asyncio
async def test_youtube_fetch_fails_when_no_transcripts_are_available(monkeypatch):
    video_ids = ["v1", "v2", "v3"]

    async def fake_search_videos(query, max_results):
        return video_ids

    async def fake_get_video_metadata(ids, target_count):
        return [_video(video_id) for video_id in ids]

    def fake_get_transcript_with_reason(video_id):
        return None, "transcripts disabled by video owner"

    monkeypatch.setattr(youtube_node.youtube_client, "search_videos", fake_search_videos)
    monkeypatch.setattr(youtube_node.youtube_client, "get_video_metadata", fake_get_video_metadata)
    monkeypatch.setattr(
        youtube_node.youtube_client,
        "get_transcript_with_reason",
        fake_get_transcript_with_reason,
    )

    result = await youtube_node.youtube_fetch({"query": "test query", "sources": {}})
    youtube_result = result["sources"]["youtube"]

    assert youtube_result["status"] == "failed"
    assert youtube_result["documents"] == []
    assert len(youtube_result["skipped"]) == 3
    assert "No YouTube transcripts" in youtube_result["error"]
