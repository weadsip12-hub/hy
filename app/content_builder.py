from __future__ import annotations
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from app.drive_manager import DriveImage


@dataclass
class BuildResult:
    post_path: str
    post_slug: str
    image_paths: List[str]


@dataclass
class ContentBuilder:
    posts_dir: Path
    images_dir: Path
    baseurl: str = ""

    def _make_slug(self, title: str) -> str:
        title = title.strip()
        title = re.sub(r"\s+", "-", title)
        title = re.sub(r"[^0-9A-Za-z가-힣\-]+", "", title)
        title = title.strip("-")
        return title[:50] if title else "post"

    def _extract_title(self, post_text: str) -> str:
        if not post_text:
            return "Untitled"
        return post_text.strip().splitlines()[0].strip() or "Untitled"

    def _today_prefix(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _ensure_dirs(self) -> None:
        self.posts_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def _copy_images(self, images: List[DriveImage], slug: str) -> List[str]:
        target_dir = self.images_dir / slug
        target_dir.mkdir(parents=True, exist_ok=True)

        out_paths: List[str] = []
        for img in images:
            src = Path(img.local_path)

            # ✅ 파일명 안전화: 공백/한글/특수문자 -> _
            safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", src.name)

            dst = target_dir / safe_name
            shutil.copy2(src, dst)
            out_paths.append(str(dst))

        return out_paths


    def _strip_front_matter(self, text: str) -> str:
        return re.sub(r"^---[\s\S]*?---\s*", "", text, flags=re.MULTILINE).lstrip()

    def _inject_images(self, text: str, image_web_paths: List[str]) -> str:
        result = text

        for i in range(1, 5):
            token = f"[[IMAGE_{i}]]"
            if i <= len(image_web_paths):
                result = result.replace(token, image_web_paths[i - 1])
            else:
                result = result.replace(token, "")

        # 이미지 붙는 현상 방지 (강제 줄바꿈)
        result = re.sub(r"\)\s*!\[", ")\n\n![", result)
        return result
    def _render_image_block(self, image_web_paths: List[str], captions_json: Dict[str, Any]) -> str:
        items = captions_json.get("images", []) if isinstance(captions_json, dict) else []

        lines: List[str] = []
        # 첫 번째 사진 위에 블로그 스타일 헤더 추가
        if image_web_paths:
            lines.append("🧡 운정에서 발견한 팥빙수 맛집")
            lines.append("")
        
        for i, url in enumerate(image_web_paths, start=1):
            alt = f"사진 {i}"
            if i - 1 < len(items) and isinstance(items[i - 1], dict):
                summary = (items[i - 1].get("summary") or "").strip()
                if summary:
                    alt = summary

            lines.append(f"![{alt}]({url})")
            lines.append("")

        return "\n".join(lines).strip()

    def _make_markdown(self, title: str, post_text: str, image_web_paths: List[str], captions_json: Dict[str, Any]) -> str:
        body = self._strip_front_matter(post_text or "")
        body = self._inject_images(body, image_web_paths)

        # 사진 alt에 캡션 추가
        img_block = self._render_image_block(image_web_paths, captions_json)
        if img_block:
            body = img_block + "\n\n---\n\n" + body.strip()

        md = []
        md.append("---")
        md.append(f'title: "{title}"')
        md.append("layout: post")
        md.append("categories: [blog]")
        md.append("---")
        md.append("")
        md.append(body.strip())
        md.append("")
        return "\n".join(md)


    def build(self, captions_json: Dict[str, Any], post_text: str, images: List[DriveImage]) -> BuildResult:
        self._ensure_dirs()

        title = self._extract_title(post_text)
        base_slug = self._make_slug(title) or "post"

        suffix = "post"
        if images and getattr(images[0], "file_id", None):
            suffix = str(images[0].file_id)[:6]

        slug = f"{base_slug}-{suffix}"

        copied_local_paths = self._copy_images(images, slug)
        base = (self.baseurl or "").rstrip("/")
        image_web_paths = [f"/blog/assets/images/{slug}/{Path(p).name}" for p in copied_local_paths]


        date_prefix = self._today_prefix()
        post_filename = f"{date_prefix}-{slug}.md"
        post_path = self.posts_dir / post_filename

        md = self._make_markdown(title, post_text, image_web_paths, captions_json)

        post_path.write_text(md, encoding="utf-8")

        return BuildResult(
            post_path=str(post_path),
            post_slug=slug,
            image_paths=copied_local_paths,
        )


def create_content_builder(config: Dict[str, Any]) -> ContentBuilder:
    base_dir = Path(__file__).resolve().parent.parent
    blog_cfg = config.get("blog", {})
    posts_path = blog_cfg.get("posts_path", "blog/posts")
    images_path = blog_cfg.get("images_path", "blog/assets/images")
    baseurl = blog_cfg.get("baseurl", "") 
    
    return ContentBuilder(
        posts_dir=base_dir / posts_path,
        images_dir=base_dir / images_path,
        baseurl=baseurl,  # ✅ 추가
    )