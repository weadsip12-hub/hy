from __future__ import annotations  # 타입 힌트 안정화
import re  # slug 만들 때 사용
import shutil  # 파일 복사/이동
from dataclasses import dataclass  # 간단 구조
from datetime import datetime  # 파일명 날짜
from pathlib import Path  # 경로 처리
from typing import Any, Dict, List, Tuple  # 타입 힌트

from app.drive_manager import DriveImage  # 이미지 구조 재사용


@dataclass
class BuildResult:  # content_builder 결과(다음 단계에 넘길 정보)
    post_path: str  # 생성된 마크다운 파일 경로
    post_slug: str  # slug(이미지 폴더명 등)
    image_paths: List[str]  # 정리된 이미지 경로 목록


@dataclass
class ContentBuilder:  # 마크다운/이미지 파일 생성 담당
    posts_dir: Path  # blog/_posts 경로
    images_dir: Path  # blog/assets/images 경로

    def _make_slug(self, title: str) -> str:  # 제목 기반 slug 생성
        title = title.strip()  # 앞뒤 공백 제거
        title = re.sub(r"\s+", "-", title)  # 공백을 -로
        title = re.sub(r"[^0-9A-Za-z가-힣\-]+", "", title)  # 허용 문자만 남기기
        title = title.strip("-")  # 양끝 - 제거
        return title[:50] if title else "post"  # 너무 길면 자르고, 비면 post

    def _extract_title(self, post_text: str) -> str:  # post_text 첫 줄을 제목으로 사용
        first = post_text.strip().splitlines()[0].strip()  # 첫 줄 가져오기
        return first if first else "Untitled"  # 비면 기본값

    def _today_prefix(self) -> str:  # Jekyll 포스트 파일명 prefix 날짜(YYYY-MM-DD)
        return datetime.now().strftime("%Y-%m-%d")  # 오늘 날짜 반환

    def _ensure_dirs(self) -> None:  # 필요한 폴더 생성
        self.posts_dir.mkdir(parents=True, exist_ok=True)  # _posts 폴더 생성
        self.images_dir.mkdir(parents=True, exist_ok=True)  # images 폴더 생성

    def _copy_images(self, images: List[DriveImage], slug: str) -> List[str]:  # incoming 이미지를 slug 폴더로 정리
        target_dir = self.images_dir / slug  # blog/assets/images/<slug>
        target_dir.mkdir(parents=True, exist_ok=True)  # 폴더 생성
        out_paths: List[str] = []  # 반환할 경로 리스트

        for img in images:  # 이미지별 처리
            if not img.local_path:  # 로컬 경로 없으면
                raise ValueError(f"Image local_path missing for file_id={img.file_id}")  # 에러
            src = Path(img.local_path)  # 원본 경로
            if not src.exists():  # 파일이 없으면
                raise FileNotFoundError(f"Local image not found: {src}")  # 에러
            dst = target_dir / src.name  # 대상 경로(파일명 유지)
            shutil.copy2(src, dst)  # 복사(메타데이터 유지)
            out_paths.append(str(dst))  # 절대/상대 경로 저장(여기선 로컬 경로)

        return out_paths  # 정리된 이미지 로컬 경로 반환

    def _make_markdown(self, title: str, slug: str, captions_json: Dict[str, Any], post_text: str, image_web_paths: List[str]) -> str:  # md 본문 생성
        lines: List[str] = []  # 라인 리스트

        # --- Jekyll front matter (필요 최소) ---
        lines.append("---")  # 시작
        lines.append(f'title: "{title}"')  # 제목
        lines.append("layout: post")  # 레이아웃
        lines.append("categories: [blog]")  # 카테고리(원하면 config로 뺄 수 있음)
        lines.append("---")  # 끝
        lines.append("")  # 빈 줄

        # --- 이미지 섹션 ---
        if image_web_paths:  # 이미지가 있으면
            lines.append("## 사진")  # 섹션 제목
            lines.append("")  # 빈 줄
            for p in image_web_paths:  # 웹 경로 기준으로
                lines.append(f"![]({p})")  # 마크다운 이미지 삽입
                lines.append("")  # 이미지 사이 빈 줄

        # --- 캡션 섹션(2줄 소개) ---
        images = captions_json.get("images", [])  # 캡션 리스트
        if images:  # 있으면
            lines.append("## 사진 한줄/두줄 소개")  # 섹션 제목
            lines.append("")  # 빈 줄
            for item in images:  # 각 캡션
                idx = item.get("index")  # 인덱스
                l1 = item.get("line1", "")  # 첫 줄
                l2 = item.get("line2", "")  # 둘째 줄
                lines.append(f"- 사진 {idx}: {l1}")  # 첫 줄 출력
                lines.append(f"  - {l2}")  # 둘째 줄 들여쓰기
            lines.append("")  # 빈 줄

        # --- 본문 ---
        lines.append("## 본문")  # 섹션 제목
        lines.append("")  # 빈 줄
        lines.append(post_text.strip())  # AI가 준 본문(더미든 실제든)
        lines.append("")  # 마지막 빈 줄

        return "\n".join(lines)  # 최종 md 문자열 반환

    def build(self, captions_json: Dict[str, Any], post_text: str, images: List[DriveImage]) -> BuildResult:  # 외부에서 호출하는 메인 함수
        self._ensure_dirs()  # 폴더 준비

        title = self._extract_title(post_text)  # 제목 추출
        base_slug = self._make_slug(title)  # 제목 기반 slug
        base_slug = base_slug.strip("-")
    

        if not base_slug:
            base_slug = "post"

        # --- slug 유니크 처리: Drive file_id 일부를 suffix로 붙여 중복 방지 ---
        suffix = "post"
        if images and getattr(images[0], "file_id", None):
            suffix = str(images[0].file_id)[:6].strip("-")

        slug = f"{base_slug}-{suffix}"

        copied_local_paths = self._copy_images(images, slug)  # 이미지 정리(복사)
        # Jekyll에서 접근할 웹 경로로 변환(루트 기준)  # blog/assets/images/<slug>/<file>
        image_web_paths = [f"blog/assets/images/{slug}/{Path(p).name}" for p in copied_local_paths]  # 웹 경로 목록

        date_prefix = self._today_prefix()  # YYYY-MM-DD
        post_filename = f"{date_prefix}-{slug}.md"  # Jekyll 포스트 파일명 규칙
        post_path = self.posts_dir / post_filename  # 실제 파일 경로

        md = self._make_markdown(title, slug, captions_json, post_text, image_web_paths)  # md 생성
        post_path.write_text(md, encoding="utf-8")  # 파일 저장

        return BuildResult(post_path=str(post_path), post_slug=slug, image_paths=copied_local_paths)  # 결과 반환


def create_content_builder(config: Dict[str, Any]) -> ContentBuilder:
    base_dir = Path(__file__).resolve().parent.parent
    blog_cfg = config.get("blog", {})
    # posts_path 기본값을 'blog/posts'로 변경
    posts_path = blog_cfg.get("posts_path", "blog/posts") 
    images_path = blog_cfg.get("images_path", "blog/assets/images")
    return ContentBuilder(posts_dir=base_dir / posts_path, images_dir=base_dir / images_path)


if __name__ == "__main__":  # 단독 테스트(Drive->더미AI->md생성)
    import json  # 출력용
    from app.config_loader import load_config  # 설정 로드
    from app.state_client import create_state_client, _build_drive_service  # Drive 인증
    from app.drive_manager import create_drive_manager  # 신규 이미지 다운로드
    from app.ai_processor import create_ai_processor  # 더미 AI

    cfg = load_config()  # config 로드
    service = _build_drive_service()  # Drive service 생성
    state = create_state_client(cfg)  # state client 생성
    dm = create_drive_manager(cfg, service)  # drive manager 생성

    new_imgs = dm.pick_new_images(state)  # 신규 이미지 선택
    if not new_imgs:  # 없으면
        print("No new images found.")  # 안내
        raise SystemExit(0)  # 종료

    downloaded = dm.download_images(new_imgs, subdir="incoming")  # 로컬 다운로드
    ai = create_ai_processor(cfg)  # AI(더미모드)
    captions = ai.generate_photo_captions(downloaded)  # 캡션 JSON
    post = ai.generate_post_markdown(captions)  # 본문 텍스트

    builder = create_content_builder(cfg)  # content builder 생성
    result = builder.build(captions, post, downloaded)  # md+이미지 생성
    print("POST CREATED:", result.post_path)  # 생성된 포스트 경로
    print("SLUG:", result.post_slug)  # slug 출력
    print("IMAGES:", json.dumps(result.image_paths, ensure_ascii=False, indent=2))  # 이미지 경로 출력

