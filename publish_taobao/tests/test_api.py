from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taobao_publisher.api import PublishRequest, resolve_video
from taobao_publisher.automation import is_video_upload_request


class PublishRequestTests(unittest.TestCase):
    def test_prepare_is_the_safe_default(self) -> None:
        request = PublishRequest(video="clip.mp4", title="标题", description="描述")
        self.assertEqual(request.mode, "prepare")
        self.assertFalse(request.confirm_publish)

    def test_video_only_request_is_valid(self) -> None:
        request = PublishRequest(video="clip.mp4")
        self.assertEqual(request.title, "")
        self.assertEqual(request.description, "")

    def test_title_matches_taobao_form_limit(self) -> None:
        request = PublishRequest(video="clip.mp4", title="标" * 30)
        self.assertEqual(len(request.title), 30)
        with self.assertRaises(ValueError):
            PublishRequest(video="clip.mp4", title="标" * 31)

    def test_publish_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(ValueError):
            PublishRequest(video="clip.mp4", title="标题", description="描述", mode="publish")

    def test_publish_accepts_explicit_confirmation(self) -> None:
        request = PublishRequest(
            video="clip.mp4",
            title="标题",
            description="描述",
            mode="publish",
            confirm_publish=True,
        )
        self.assertTrue(request.confirm_publish)

    def test_trims_title_and_description(self) -> None:
        request = PublishRequest(video="clip.mp4", title=" 标题 ", description=" 描述 ")
        self.assertEqual(request.title, "标题")
        self.assertEqual(request.description, "描述")


class VideoPathTests(unittest.TestCase):
    def test_accepts_supported_video_inside_videos_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            with patch("taobao_publisher.api.VIDEO_ROOT", root):
                self.assertEqual(resolve_video("clip.mp4"), video.resolve())

    def test_rejects_unsupported_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.avi"
            video.write_bytes(b"video")
            with patch("taobao_publisher.api.VIDEO_ROOT", root):
                with self.assertRaises(ValueError):
                    resolve_video("clip.avi")

    def test_rejects_file_larger_than_taobao_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            with video.open("wb") as stream:
                stream.truncate(300_000_001)
            with patch("taobao_publisher.api.VIDEO_ROOT", root):
                with self.assertRaises(ValueError):
                    resolve_video("clip.mp4")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("taobao_publisher.api.VIDEO_ROOT", root):
                with self.assertRaises(ValueError):
                    resolve_video("../outside.mp4")


class UploadRequestClassificationTests(unittest.TestCase):
    def test_ignores_taobao_frontend_log_404(self) -> None:
        self.assertFalse(
            is_video_upload_request(
                "POST",
                "https://general-tickets.cn-wulanchabu.log.aliyuncs.com/"
                "logstores/voc-front-end-log/track",
            )
        )

    def test_accepts_video_upload_endpoints(self) -> None:
        self.assertTrue(is_video_upload_request("POST", "https://seller.taobao.com/api/video/upload"))
        self.assertTrue(is_video_upload_request("PUT", "https://bucket.oss-cn-hangzhou.aliyuncs.com/object"))

    def test_ignores_non_upload_methods(self) -> None:
        self.assertFalse(is_video_upload_request("GET", "https://seller.taobao.com/api/video/upload"))


if __name__ == "__main__":
    unittest.main()
