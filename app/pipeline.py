from __future__ import annotations
import subprocess
import traceback
import json  # 추가됨
from pathlib import Path  # 추가됨
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from PIL import Image

from app.config_loader import load_config
from app.state_client import create_state_client, _build_drive_service
from app.drive_manager import create_drive_manager, DriveImage
from app.ai_processor import create_ai_processor
from app.content_builder import create_content_builder, BuildResult
from app.git_publisher import create_git_publisher
import time

@dataclass
class PipelineResult:
    ok: bool
    message: str
    processed_count: int = 0
    post_path: Optional[str] = None
    post_slug: Optional[str] = None
    errors: Optional[List[str]] = None

class Pipeline:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._drive_service = _build_drive_service()
        self.state_client = create_state_client(config)
        self.drive_manager = create_drive_manager(config, self._drive_service)
        self.ai = create_ai_processor(config)
        self.builder = create_content_builder(config)
        self.git = create_git_publisher(config)

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{level}] {msg}")

    def _git_is_tracked(self, rel_path: str) -> bool:
        try:
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", rel_path],
                cwd=str(self.git.repo_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            return True
        except Exception:
            return False

    def _preflight_security_checks(self) -> None:
        secrets = ["client_secret.json", "token.json", ".env"]
        for s in secrets:
            if self._git_is_tracked(s):
                raise RuntimeError(f"SECURITY BLOCK: '{s}' is tracked by git. Remove it from git history and add to .gitignore.")

    def _pick_and_download(self) -> List[DriveImage]:
        self._log("INFO", "Scanning Google Drive for new images...")
        new_images = self.drive_manager.pick_new_images(self.state_client)
        if not new_images:
            self._log("INFO", "No new images found. Nothing to do.")
            return []

        self._log("INFO", f"Found {len(new_images)} new image(s). Downloading...")
        try:
            downloaded = self.drive_manager.download_images(new_images, subdir="incoming")
            for img in downloaded:
                self._log("INFO", f"Downloaded: {img.name} -> {img.local_path}")
            return downloaded
        except Exception as e:
            self._log("ERROR", f"Failed to download images: {e}")
            raise

    def _resize_images(self, downloaded: List[DriveImage]) -> None:
        """다운로드된 이미지를 리사이즈하여 크기를 줄임"""
        resize_cfg = self.config.get("image_resize", {})
        max_width = resize_cfg.get("max_width", 1024)
        max_height = resize_cfg.get("max_height", 1024)
        quality = resize_cfg.get("quality", 85)

        for img in downloaded:
            if not img.local_path:
                continue
            path = Path(img.local_path)
            if not path.exists():
                continue

            try:
                with Image.open(path) as im:
                    # 원본 크기 확인
                    orig_width, orig_height = im.size
                    if orig_width <= max_width and orig_height <= max_height:
                        self._log("INFO", f"Image {img.name} already small enough, skipping resize")
                        continue

                    # 리사이즈
                    im.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
                    # 저장 (JPEG 품질 설정)
                    if path.suffix.lower() in ('.jpg', '.jpeg'):
                        im.save(path, quality=quality)
                    else:
                        im.save(path)
                    self._log("INFO", f"Resized {img.name}: {orig_width}x{orig_height} -> {im.size}")
            except Exception as e:
                self._log("ERROR", f"Failed to resize {img.name}: {e}")

    def _ai_generate(self, downloaded: List[DriveImage]) -> tuple[Dict[str, Any], str]:
        self._log("INFO", "Generating captions (1 call for up to 4 images)...")
        captions = self.ai.generate_photo_captions(downloaded)

        #time.sleep(5)  # ✅ API 호출 사이 딜레이

        self._log("INFO", "Loading prompt from Google Drive (latest Google Docs)...")
        prompt = self.drive_manager.load_prompt_text()
        self._log("INFO", f"Prompt loaded: {len(prompt)} chars")

        self._log("INFO", "Generating post text (1 call)...")
        post_text = self.ai.generate_post_markdown(captions, prompt)

        #time.sleep(5)  # ✅ 추가 딜레이

        return captions, post_text

    
    def _transform_trendy(self, post_text: str, captions: Dict[str, Any]) -> str:
        """
        AI가 만든 글을 '트렌디 블로그 스타일'로 변환하는 단계
        """

        lines = [ln.strip() for ln in post_text.splitlines() if ln.strip()]

        # AI 보고서 스타일 제목 제거
        drop_keywords = ["목차", "총평", "Verdict", "Special Tips", "이용 꿀팁", "해시태그", "Auto Tags"]
        filtered = []
        for ln in lines:
            if any(k in ln for k in drop_keywords):
                continue
            if ln[:2].isdigit() and ". " in ln[:4]:  # "5. ..." 같은 번호 제거
                continue
            filtered.append(ln)

        # 트렌디 훅 + 감성 구조
        hook = [
            "🧡 아이랑 파주에서 진짜 괜찮았던 곳 발견",
            "",
            "솔직히 말하면,",
            "“파주에 이런 데가 있었나?” 싶었음.",
            "",
        ]

        bullets = [
            "✔ 직접 구운 빵/디저트 퀄리티",
            "✔ 아이가 좋아할 분위기",
            "✔ 주차 스트레스 거의 없음",
            "",
            "---",
            "",
        ]

        return "\n".join(hook + bullets + filtered)

    def _build_content(self, captions: Dict[str, Any], post_text: str, downloaded: List[DriveImage]) -> BuildResult:
        self._log("INFO", "Building blog content (markdown + images)...")
        result = self.builder.build(captions, post_text, downloaded)
        self._log("INFO", f"Post created: {result.post_path}")
        return result

    def _update_posts_metadata(self, build_result: BuildResult, title: str) -> None:
        """블로그 목록(index.html)이 사용하는 posts.json 업데이트"""
        self._log("INFO", "Updating posts.json manifest...")
        json_path = self.git.repo_dir / "posts.json"
        
        if json_path.exists():
            try:
                posts = json.loads(json_path.read_text(encoding="utf-8"))
            except:
                posts = []
        else:
            posts = []

        new_entry = {
            "title": title,
            "file": Path(build_result.post_path).name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "tags": ["blog"]
        }

        if not any(p.get("file") == new_entry["file"] for p in posts):
            posts.insert(0, new_entry)

        json_path.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log("INFO", "posts.json updated.")

    def _git_publish(self, build_result: BuildResult) -> None:
        git_cfg = self.config.get("git", {})
        template = git_cfg.get("commit_message_template", "chore: publish {slug}")
        msg = template.format(slug=build_result.post_slug)
        self._log("INFO", f"Publishing to GitHub (branch={self.git.branch})...")
        self.git.publish(msg)
        self._log("INFO", "GitHub publish done.")

    def _update_state(self, downloaded: List[DriveImage], slug: str) -> int:
        """구글 드라이브의 state.json에 처리 완료 마킹"""
        self._log("INFO", "Updating state.json on Google Drive (mark processed)...")
        ok_count = 0
        for img in downloaded:
            try:
                self.state_client.mark_processed(img.file_id, slug)
                ok_count += 1
            except Exception as e:
                self._log("ERROR", f"Failed to mark processed for {img.file_id}: {e}")
        self._log("INFO", f"State updated for {ok_count}/{len(downloaded)} image(s).")
        return ok_count

    def run(self) -> PipelineResult:
        errors: List[str] = []
        try:
            self._preflight_security_checks()
        except Exception as e:
            return PipelineResult(ok=False, message=str(e), errors=[str(e)])

        downloaded: List[DriveImage] = []
        build_result: Optional[BuildResult] = None

        try:
            downloaded = self._pick_and_download()
            if not downloaded:
                return PipelineResult(ok=True, message="No new images.", processed_count=0)

            self._resize_images(downloaded)

            captions, post_text = self._ai_generate(downloaded)
            #time.sleep(5)
            build_result = self._build_content(captions, post_text, downloaded)
            
            # 메타데이터 업데이트 (제목 추출 로직 포함)
            title = post_text.splitlines()[0].strip("# ")
            self._update_posts_metadata(build_result, title)

            # Git 배포
            self._git_publish(build_result)

            # 구글 드라이브 상태 업데이트 (여기서 아까 에러났던 부분!)
            marked = self._update_state(downloaded, build_result.post_slug)
            
            return PipelineResult(
                ok=True,
                message="Pipeline completed successfully.",
                processed_count=marked,
                post_path=build_result.post_path,
                post_slug=build_result.post_slug,
                errors=None,
            )

        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            errors.append(err_msg)
            self._log("ERROR", err_msg)
            self._log("ERROR", traceback.format_exc())
            return PipelineResult(
                ok=False,
                message="Pipeline failed.",
                processed_count=0,
                post_path=(build_result.post_path if build_result else None),
                post_slug=(build_result.post_slug if build_result else None),
                errors=errors,
            )

def run_pipeline() -> PipelineResult:
    cfg = load_config()
    p = Pipeline(cfg)
    return p.run()