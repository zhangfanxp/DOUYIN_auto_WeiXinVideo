from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from channels_publisher.api import PublishRequest, resolve_video


class PublishRequestTests(unittest.TestCase):
    def test_publish_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(ValueError):
            PublishRequest(video="clip.mp4", description="caption", mode="publish")

    def test_draft_is_default(self) -> None:
        request = PublishRequest(video="clip.mp4", description="caption")
        self.assertEqual(request.mode, "draft")
        self.assertFalse(request.confirm_publish)

    def test_accepts_supported_video_annotations(self) -> None:
        annotations = (
            "无需标注",
            "含AI生成内容",
            "内容为虚构剧情,仅供娱乐",
            "个人观点,仅供参考",
            "内容包含营销广告",
            "内容为自行拍摄",
            "内容为转载",
        )
        for annotation in annotations:
            with self.subTest(annotation=annotation):
                request = PublishRequest(
                    video="clip.mp4",
                    description="caption",
                    video_annotation=annotation,
                )
                self.assertEqual(request.video_annotation, annotation)

    def test_rejects_unknown_video_annotation(self) -> None:
        with self.assertRaises(ValueError):
            PublishRequest(
                video="clip.mp4",
                description="caption",
                video_annotation="其他",
            )

    def test_legacy_ai_flag_selects_ai_annotation(self) -> None:
        request = PublishRequest(
            video="clip.mp4",
            description="caption",
            contains_ai_content=True,
        )
        self.assertEqual(request.video_annotation, "含AI生成内容")


class VideoPathTests(unittest.TestCase):
    def test_accepts_video_inside_videos_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            with patch("channels_publisher.api.VIDEO_ROOT", root):
                self.assertEqual(resolve_video("clip.mp4"), video.resolve())

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("channels_publisher.api.VIDEO_ROOT", root):
                with self.assertRaises(ValueError):
                    resolve_video("../outside.mp4")


if __name__ == "__main__":
    unittest.main()
