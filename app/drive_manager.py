from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from googleapiclient.http import MediaIoBaseDownload
from io import BytesIO
from typing import Optional
from app.state_client import StateClient

IMAGE_MIME_PREFIX = "image/"

@dataclass
class DriveImage:
    file_id: str
    name: str
    mime_type: str
    modified_time: str
    local_path: Optional[str] = None

@dataclass
class DriveManager:
    drive_service: Any
    input_folder_id: str
    images_root: Path
    batch_size: int = 4

    # ✅ Input_text (Google Drive 프롬프트 폴더)
    input_text_folder_id: Optional[str] = None

    def _list_images_in_folder(self) -> List[DriveImage]:
        q = (
            f"'{self.input_folder_id}' in parents and "
            "trashed = false and "
            f"mimeType contains '{IMAGE_MIME_PREFIX}'"
        )
        resp = self.drive_service.files().list(
            q=q,
            fields="files(id,name,mimeType,modifiedTime)",
            pageSize=200,
        ).execute()

        files = resp.get("files", [])
        images: List[DriveImage] = []
        for f in files:
            images.append(
                DriveImage(
                    file_id=f["id"],
                    name=f["name"],
                    mime_type=f.get("mimeType", ""),
                    modified_time=f.get("modifiedTime", ""),
                )
            )
        images.sort(key=lambda x: x.modified_time, reverse=False)  # 오래된 순
        return images

    def pick_new_images(self, state_client: StateClient) -> List[DriveImage]:
        all_images = self._list_images_in_folder()
        new_images: List[DriveImage] = []
        for img in all_images:
            if not state_client.is_processed(img.file_id):
                new_images.append(img)
            if len(new_images) >= self.batch_size:
                break
        return new_images

    def _safe_filename(self, name: str) -> str:
        bad = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for ch in bad:
            name = name.replace(ch, "_")
        return name

    def _download_bytes(self, file_id: str) -> bytes:
        request = self.drive_service.files().get_media(fileId=file_id)
        fh = BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue()

    def download_images(self, images: List[DriveImage], subdir: str) -> List[DriveImage]:
        target_dir = self.images_root / subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        downloaded: List[DriveImage] = []
        for img in images:
            safe_name = self._safe_filename(img.name)
            local_path = target_dir / safe_name

            data = self._download_bytes(img.file_id)
            local_path.write_bytes(data)

            img.local_path = str(local_path)
            downloaded.append(img)

        return downloaded

    # ✅ Input_text 폴더에서 "최신 수정된 Google Docs 1개"를 프롬프트로 읽기
    def load_prompt_text(self) -> str:
        if not self.input_text_folder_id:
            return ""

        q = (
            f"'{self.input_text_folder_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.document' and "
            "trashed = false"
        )
        resp = self.drive_service.files().list(
            q=q,
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=1,
        ).execute()

        files = resp.get("files", [])
        if not files:
            return ""
        doc_name = files[0]["name"]
        print(f"[PROMPT] Using Google Docs: {doc_name}")

        file_id = files[0]["id"]

        request = self.drive_service.files().export_media(
            fileId=file_id,
            mimeType="text/plain",
        )
        fh = BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        return fh.getvalue().decode("utf-8", errors="replace").strip()

    # ✅ 기존 함수명 호환 (pipeline이 아직 load_notepad_text() 쓰고 있어도 OK)
    def load_notepad_text(self) -> str:
        return self.load_prompt_text()


def create_drive_manager(config: Dict[str, Any], drive_service: Any) -> DriveManager:
    drive_cfg = config.get("drive", {})
    blog_cfg = config.get("blog", {})
    pipeline_cfg = config.get("pipeline", {})

    input_folder_id = drive_cfg.get("input_folder_id")
    if not input_folder_id:
        raise ValueError("config.drive.input_folder_id is required")

    images_path = blog_cfg.get("images_path", "blog/assets/images")
    batch_size = int(pipeline_cfg.get("batch_size", 4))

    # ✅ Input_text 설정(없어도 동작)
    input_text_folder_id = drive_cfg.get("input_text_folder_id")

    base_dir = Path(__file__).resolve().parent.parent
    images_root = base_dir / images_path

    return DriveManager(
        drive_service=drive_service,
        input_folder_id=input_folder_id,
        images_root=images_root,
        batch_size=batch_size,
        input_text_folder_id=input_text_folder_id,
    )


if __name__ == "__main__":
    from app.config_loader import load_config
    from app.state_client import create_state_client, _build_drive_service

    cfg = load_config()
    service = _build_drive_service()
    state = create_state_client(cfg)
    mgr = create_drive_manager(cfg, service)

    new_imgs = mgr.pick_new_images(state)
    print(f"New images: {len(new_imgs)}")
    for i in new_imgs:
        print(i.file_id, i.name, i.modified_time)

    memo = mgr.load_notepad_text()
    print("NOTEPAD:")
    print(memo)

    if new_imgs:
        downloaded = mgr.download_images(new_imgs, subdir="incoming")
        print("Downloaded:")
        for d in downloaded:
            print(d.local_path)
