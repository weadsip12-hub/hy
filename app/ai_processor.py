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
    model: str
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

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        params = {"key": self.api_key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        r = requests.post(url, params=params, json=payload, timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"Gemini API error: {r.status_code} {r.text}")

        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data, ensure_ascii=False)[:800]}")

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

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        params = {"key": self.api_key}
        payload = {
            "contents": [{"role": "user", "parts": fixed_parts}],
            "generationConfig": {
                "temperature": 0.4,
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
        if self.mock_mode:
            return self._mock_post(captions)

        if self.provider.lower() != "gemini":
            raise RuntimeError(f"Real mode supports only provider='gemini' for now (got {self.provider})")

        writer_prompt = self._read_prompt("post_writer.txt")
        input_payload = json.dumps(captions, ensure_ascii=False, indent=2)

        user_payload = []
        user_payload.append("[CAPTIONS_JSON]\n" + input_payload)

        if notepad and notepad.strip():
            user_payload.append("[INPUT_TEXT]\n" + notepad.strip())

        final_user_text = "\n\n".join(user_payload)

        prompt = (
            f"{writer_prompt}\n\n"
            f"{final_user_text}\n\n"
            f"위 JSON의 images 배열 순서대로 사진 섹션을 만들고, "
            f"각 사진에 대해 이미지 1에는 [[IMAGE_1]] … 형태를 포함해. "
            f"IMAGE_PATH는 나중에 파이프라인이 넣을 거라서, 여기선 'IMAGE_PATH' 그대로 써."
        )


        return self._gemini_generate_text(prompt, temperature=0.6, max_tokens=1400).strip()


def create_ai_processor(config: Dict[str, Any]) -> AIProcessor:
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "gemini")
    model = ai_cfg.get("model", "gemini-2.0-flash")
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
        model=model,
        api_key=api_key,
        prompts_dir=prompts_dir,
        mock_mode=mock_mode,
    )
