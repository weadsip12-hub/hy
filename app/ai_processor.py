from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import requests  # ✅ pip install requests 필요

from app.drive_manager import DriveImage


@dataclass
class AIProcessor:
    provider: str    
    vision_model: str   # ✅ 이미지/캡션
    text_model: str     # ✅ 글/리라이팅
    api_key: str | None
    prompts_dir: Path
    mock_mode: bool = False

    def _read_prompt(self, filename: str) -> str:
        path = self.prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing prompt file: {path}")
        return path.read_text(encoding="utf-8")

    # -----------------------------
    # ✅ 더미 생성 로직
    # -----------------------------
    def _mock_captions(self, images: List[DriveImage]) -> Dict[str, Any]:
        return {
            "images": [
                {
                    "index": idx,
                    "line1": f"(더미) 사진 {idx} 한 줄 소개",
                    "line2": f"(더미) 사진 {idx} 두 번째 줄 소개",
                }
                for idx in range(1, len(images) + 1)
            ]
        }

    def _mock_post(self, captions_json: Dict[str, Any]) -> str:
        lines = []
        lines.append("더미 제목: 자동 블로그 포스팅 테스트")
        lines.append("")
        lines.append("오늘은 자동화 파이프라인을 더미모드로 테스트했어.")
        lines.append("")
        lines.append("사진별 요약:")
        for item in captions_json.get("images", []):
            lines.append(f"- 사진 {item['index']}: {item['line1']} / {item['line2']}")
        lines.append("")
        lines.append("마무리: AI 연결되면 여기 내용이 실제 글로 바뀔 거야.")
        return "\n".join(lines)

    # -----------------------------
    # ✅ 실모드: DriveImage에서 로컬 파일 경로 뽑기(필드명 달라도 안전)
    # -----------------------------
    def _get_local_path(self, img: DriveImage) -> Path:
        # DriveImage가 local_path / path / local_file 같은 이름 중 뭐든 쓸 수 있게 방어
        for key in ("local_path", "path", "local_file", "download_path"):
            v = getattr(img, key, None)
            if v:
                return Path(v)
        raise AttributeError("DriveImage has no local path field (expected one of local_path/path/local_file/download_path)")

    # -----------------------------
    # ✅ 실모드: Gemini 호출 (텍스트)
    # -----------------------------
    def _gemini_generate_text(self, prompt: str, temperature: float = 0.6, max_tokens: int = 1200) -> str:
        if not self.api_key:
            raise ValueError("Missing API key: set GEMINI_API_KEY (or set ai.mock_mode=true)")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.text_model}:generateContent"
        params = {"key": self.api_key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        import time, random

        # ✅ 429 / 이상응답(MAX_TOKENS + parts없음) 자동 재시도
        for attempt in range(4):  # 최대 4번
            r = requests.post(url, params=params, json=payload, timeout=90)

            # HTTP 에러 처리
            if r.status_code == 429:
                wait = (1.2 * (2 ** attempt)) + random.uniform(0.0, 0.8)
                print(f"[WARN] Gemini 429. retry in {wait:.1f}s...")
                time.sleep(wait)
                continue

            if r.status_code != 200:
                raise RuntimeError(f"Gemini API error: {r.status_code} {r.text}")

            data = r.json()

            # ✅ 텍스트 안전 추출
            cands = data.get("candidates") or []
            cand0 = cands[0] if cands else {}
            content = (cand0.get("content") or {})
            parts = content.get("parts") or []

            texts = []
            for p in parts:
                if isinstance(p, dict) and p.get("text"):
                    texts.append(p["text"])

            if texts:
                return "\n".join(texts).strip()

            # ✅ 여기로 오면: parts가 없거나 text가 없음
            # 로그에 네가 본 케이스: finishReason=MAX_TOKENS, content.parts 없음
            finish = cand0.get("finishReason")

            # MAX_TOKENS면서 텍스트가 비어있으면 -> 잠깐 쉬고 재시도
            if finish == "MAX_TOKENS":
                wait = (1.0 * (2 ** attempt)) + random.uniform(0.0, 0.6)
                print(f"[WARN] finishReason=MAX_TOKENS but no text parts. retry in {wait:.1f}s...")
                time.sleep(wait)
                continue

            # 그 외는 그냥 에러로 보여주기
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data, ensure_ascii=False)[:800]}")

        raise RuntimeError("Gemini text generation failed after retries (429/MAX_TOKENS/no parts)")

    # -----------------------------
    # ✅ 실모드: Gemini 호출 (이미지 + 텍스트 → 캡션 JSON)
    # -----------------------------
    def _gemini_generate_captions_json(self, images: List[DriveImage]) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("Missing API key: set GEMINI_API_KEY (or set ai.mock_mode=true)")

        prompt = self._read_prompt("photo_captions.txt")

        parts: List[Dict[str, Any]] = [{"text": prompt}]

        # 최대 4장
        for img in images[:4]:
            p = self._get_local_path(img)
            b = p.read_bytes()

            mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": b.hex(),  # ❌ 이건 안됨 (바로 아래에서 base64로 바꿈)
                    }
                }
            )

        # 위에서 hex로 넣으면 안 되니까 base64로 교체
        # (parts를 다시 만들어서 넣는 방식으로 안전 처리)
        import base64
        fixed_parts: List[Dict[str, Any]] = [{"text": prompt}]
        for img in images[:4]:
            p = self._get_local_path(img)
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            fixed_parts.append(
                {"inline_data": {"mime_type": mime, "data": b64}}
            )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.vision_model}:generateContent"
        params = {"key": self.api_key}
        payload = {
            "contents": [{"role": "user", "parts": fixed_parts}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 900,
            },
        }

        r = requests.post(url, params=params, json=payload, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Gemini API error: {r.status_code} {r.text}")

        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data, ensure_ascii=False)[:800]}")

        cleaned = text.strip().replace("```json", "").replace("```", "").strip()

        try:
            return json.loads(cleaned)
        except Exception:
            raise RuntimeError(f"Captions JSON parse failed. Raw={cleaned[:800]}")

    # -----------------------------
    # ✅ 외부에서 쓰는 메인 함수 2개
    # -----------------------------
    def generate_photo_captions(self, images: List[DriveImage]) -> Dict[str, Any]:
        if not images:
            raise ValueError("generate_photo_captions: images is empty")
        images = images[:4]

        if self.mock_mode:
            return self._mock_captions(images)

        # 실모드
        if self.provider.lower() != "gemini":
            raise RuntimeError(f"Real mode supports only provider='gemini' for now (got {self.provider})")

        return self._gemini_generate_captions_json(images)

    def generate_post_markdown(self, captions: Dict[str, Any], notepad: str = "") -> str:
        # 1. 테스트 모드 대응
        if self.mock_mode:
            return self._mock_post(captions)

        # 2. 프로바이더 체크
        if self.provider.lower() != "gemini":
            raise RuntimeError(f"Real mode supports only provider='gemini' for now (got {self.provider})")

        # 3. 프롬프트 파일 읽기 (수정하기 쉽게 외부로 빼둔 파일)
        writer_prompt = self._read_prompt("post_writer.txt")
        
        # 4. 입력 데이터 구조화 (JSON으로 묶어서 보내야 토큰이 절약되고 AI가 잘 알아듣습니다)
        user_payload = {
            "captions": captions,
            "additional_info": notepad.strip() if notepad else ""
        }
        input_data_str = json.dumps(user_payload, ensure_ascii=False, indent=2)

        # 5. 최종 프롬프트 구성 (아주 깔끔해졌죠?)
        # 지시사항(파일 내용) + 실제 데이터(JSON)
        final_prompt = (
            f"{writer_prompt}\n\n"
            f"### INPUT DATA (JSON):\n{input_data_str}\n\n"
            f"---"
        )

        # 6. 실행 및 결과 반환
        return self._gemini_generate_text(final_prompt, temperature=0.6, max_tokens=1400).strip()

    def rewrite_trendy_blog(self, draft_post: str, style_note: str = "") -> str:
        """
        2차 호출: 초안(draft_post)을 '트렌디 블로그' 톤으로 리라이팅한다.
        - 보고서/목차/번호 섹션 제거
        - 짧은 문장 + 줄바꿈
        - 이모지 훅 + 체크포인트 + 구분선
        - 과한 영어/형식적 소제목 금지
        """
        if self.mock_mode:
            # 더미 모드에서는 그냥 초안 그대로 반환
            return draft_post

        if self.provider.lower() != "gemini":
            raise RuntimeError(f"Real mode supports only provider='gemini' for now (got {self.provider})")

        rewrite_prompt = self._read_prompt("post_rewriter_trendy.txt")

        payload = {
            "draft_post": draft_post,
            "style_note": style_note.strip() if style_note else ""
        }

        final_prompt = (
            f"{rewrite_prompt}\n\n"
            f"### INPUT (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"---"
        )

        return self._gemini_generate_text(final_prompt, temperature=0.7, max_tokens=1500).strip()

def create_ai_processor(config: Dict[str, Any]) -> AIProcessor:
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "gemini")
    vision_model = ai_cfg.get("vision_model", "gemini-2.0-flash")
    text_model = ai_cfg.get("text_model", "gemini-2.5-pro")
    mock_mode = bool(ai_cfg.get("mock_mode", False))

    base_dir = Path(__file__).resolve().parent.parent
    prompts_dir = base_dir / "prompts"

    api_key = None
    if not mock_mode:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("AI_API_KEY")
        if not api_key:
            raise ValueError("Missing API key: set GEMINI_API_KEY (or set ai.mock_mode=true)")

    return AIProcessor(
        provider=provider,
        vision_model=vision_model,
        text_model=text_model,
        api_key=api_key,
        prompts_dir=prompts_dir,
        mock_mode=mock_mode,
    )
