from __future__ import annotations  # 타입 힌트 안정화
import json  # state.json 직렬화/역직렬화
import os  # 환경변수 읽기
from dataclasses import dataclass  # 간단한 데이터 구조용
from datetime import datetime, timezone  # 처리 시각 기록용(UTC)
from io import BytesIO  # Drive 다운로드/업로드 버퍼
from typing import Any, Dict, Optional  # 타입 힌트

from googleapiclient.discovery import build  # Drive API client 생성
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload  # 파일 다운로드/업로드
from google.oauth2.credentials import Credentials  # OAuth 토큰 기반 인증
from google.oauth2.service_account import Credentials as SACredentials  # 서비스계정 인증
from google_auth_oauthlib.flow import InstalledAppFlow  # 로컬 OAuth 로그인 플로우
from google.auth.transport.requests import Request  # 토큰 갱신 요청


SCOPES = ["https://www.googleapis.com/auth/drive"]  # Drive 읽기/쓰기 권한(최소 필요 권한)


@dataclass
class StateClient:  # state.json을 Drive에서 관리하는 클라이언트
    drive_service: Any  # google drive service 객체
    state_folder_id: str  # state.json이 위치할 Drive 폴더 ID
    state_file_name: str = "state.json"  # state 파일명 기본값
    state_file_id: Optional[str] = None  # state.json의 Drive 파일 ID(찾아두면 캐시)

    def _now_utc_iso(self) -> str:  # 현재 시간을 UTC ISO 문자열로 반환
        return datetime.now(timezone.utc).isoformat()  # 예: 2026-01-23T06:00:00+00:00

    def _find_state_file_id(self) -> Optional[str]:  # 폴더 내 state.json 파일 ID를 찾는다
        q = (  # Drive 검색 쿼리 문자열
            f"'{self.state_folder_id}' in parents and "  # 특정 폴더 안에서
            f"name = '{self.state_file_name}' and "  # 파일명이 state.json이고
            "trashed = false"  # 휴지통이 아니면
        )
        resp = self.drive_service.files().list(q=q, fields="files(id, name)").execute()  # 파일 목록 조회
        files = resp.get("files", [])  # 결과에서 files 추출
        if not files:  # 없으면
            return None  # None 반환
        return files[0]["id"]  # 첫 번째 파일 ID 반환(동명이 파일 여러 개면 첫 번째 사용)

    def _create_empty_state_file(self) -> str:  # 폴더에 state.json이 없으면 새로 만든다
        empty_state = {"version": 1, "processed": []}  # 최소 state 구조
        data = json.dumps(empty_state, ensure_ascii=False, indent=2).encode("utf-8")  # JSON bytes 생성

        media = MediaIoBaseUpload(BytesIO(data), mimetype="application/json", resumable=False)  # 업로드 미디어 생성
        metadata = {  # Drive 파일 메타데이터
            "name": self.state_file_name,  # 파일명
            "parents": [self.state_folder_id],  # 부모 폴더
            "mimeType": "application/json",  # JSON 파일
        }
        created = self.drive_service.files().create(body=metadata, media_body=media, fields="id").execute()  # 파일 생성
        return created["id"]  # 생성된 파일 ID 반환

    def ensure_state_file(self) -> str:  # state.json 파일이 존재하도록 보장하고 file_id를 반환
        if self.state_file_id:  # 이미 캐시되어 있으면
            return self.state_file_id  # 그대로 반환
        file_id = self._find_state_file_id()  # Drive에서 검색
        if file_id is None:  # 없으면
            file_id = self._create_empty_state_file()  # 새로 생성
        self.state_file_id = file_id  # 캐시 저장
        return file_id  # file_id 반환

    def download_state(self) -> Dict[str, Any]:  # Drive에서 state.json 내려받아 dict로 반환
        file_id = self.ensure_state_file()  # state.json file_id 확보
        request = self.drive_service.files().get_media(fileId=file_id)  # 다운로드 요청 생성
        fh = BytesIO()  # 메모리 버퍼
        downloader = MediaIoBaseDownload(fh, request)  # 다운로드 객체 생성
        done = False  # 완료 여부
        while not done:  # 완료될 때까지 반복
            _, done = downloader.next_chunk()  # 다음 청크 다운로드
        content = fh.getvalue().decode("utf-8")  # bytes -> str
        data = json.loads(content)  # JSON -> dict
        if "processed" not in data or not isinstance(data["processed"], list):  # 필수 구조 검증
            raise ValueError("Invalid state.json: missing 'processed' list")  # 구조가 이상하면 에러
        data.setdefault("version", 1)  # version 없으면 기본 추가
        return data  # state dict 반환

    def upload_state(self, state: Dict[str, Any]) -> None:  # dict state를 Drive에 업로드(덮어쓰기)
        file_id = self.ensure_state_file()  # state.json file_id 확보
        data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")  # dict -> JSON bytes
        media = MediaIoBaseUpload(BytesIO(data), mimetype="application/json", resumable=False)  # 업로드 미디어 생성
        self.drive_service.files().update(fileId=file_id, media_body=media).execute()  # 파일 내용 업데이트

    def is_processed(self, drive_file_id: str) -> bool:  # 특정 Drive 파일이 이미 처리됐는지 확인
        state = self.download_state()  # state 다운로드
        for item in state["processed"]:  # 처리 목록 순회
            if item.get("file_id") == drive_file_id:  # file_id가 같으면
                return True  # 이미 처리됨
        return False  # 처리되지 않음

    def mark_processed(self, drive_file_id: str, post_slug: str) -> None:  # 처리 완료 기록 추가 후 업로드
        state = self.download_state()  # state 다운로드
        if any(item.get("file_id") == drive_file_id for item in state["processed"]):  # 중복 방지
            return  # 이미 있으면 아무 것도 안 함
        state["processed"].append({  # 처리 기록 추가
            "file_id": drive_file_id,  # Drive 파일 ID
            "post_slug": post_slug,  # 생성된 포스트 slug
            "processed_at": self._now_utc_iso(),  # 처리 시각(UTC)
        })
        self.upload_state(state)  # 업로드(저장)


def _build_drive_service() -> Any:  # Drive API service 객체를 만든다(서비스계정 or OAuth)
    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")  # 서비스계정 키 JSON 경로(환경변수)
    if sa_path and os.path.exists(sa_path):  # 서비스계정 키가 있으면
        creds = SACredentials.from_service_account_file(sa_path, scopes=SCOPES)  # 서비스계정 creds 생성
        return build("drive", "v3", credentials=creds)  # drive service 생성

    # ✅ repo 루트 기준으로 경로를 고정 (작업 디렉토리(os.getcwd())에 영향 안 받게)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # .../hyun/app -> .../hyun

    # ✅ token은 루트에 저장(추적 금지), client_secret은 루트 또는 secret 폴더 허용
    token_path = os.path.join(base_dir, "token.json")
    client_secret_candidates = [
        os.path.join(base_dir, "client_secret.json"),
        os.path.join(base_dir, "secrets", "client_secret.json"),
    ]
    client_secret_path = next((p for p in client_secret_candidates if os.path.exists(p)), None)

    creds: Optional[Credentials] = None  # creds 초기화

    if os.path.exists(token_path):  # token.json이 있으면
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)  # token으로 creds 생성

    if not creds or not creds.valid:  # creds가 없거나 유효하지 않으면
        if creds and creds.expired and creds.refresh_token:  # 만료됐고 refresh_token 있으면
            creds.refresh(Request())  # 토큰 갱신
        else:  # 처음 로그인 필요
            if not client_secret_path:  # client_secret.json이 없으면
                raise FileNotFoundError(
                    "Missing OAuth client secret file. Checked:\n"
                    f"- {client_secret_candidates[0]}\n"
                    f"- {client_secret_candidates[1]}\n"
                    "Or set GOOGLE_APPLICATION_CREDENTIALS for service account."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)  # 로컬 로그인 플로우 준비
            creds = flow.run_local_server(port=0)  # 브라우저 열어서 로그인(자동)

        with open(token_path, "w", encoding="utf-8") as f:  # 갱신/발급된 토큰 저장
            f.write(creds.to_json())  # token.json 생성/업데이트

    return build("drive", "v3", credentials=creds)  # drive service 생성


def create_state_client(config: Dict[str, Any]) -> StateClient:  # config로 StateClient 생성
    drive_cfg = config.get("drive", {})  # config.drive 섹션 가져오기
    folder_id = drive_cfg.get("state_folder_id")  # state_folder_id 읽기
    if not folder_id:  # 없으면
        raise ValueError("config.drive.state_folder_id is required")  # 명확히 에러
    file_name = drive_cfg.get("state_file_name", "state.json")  # 파일명 기본 state.json
    service = _build_drive_service()  # Drive service 생성
    return StateClient(drive_service=service, state_folder_id=folder_id, state_file_name=file_name)  # 객체 반환


if __name__ == "__main__":  # 단독 실행 테스트용
    from app.config_loader import load_config  # config_loader 사용

    cfg = load_config()  # config 로드
    client = create_state_client(cfg)  # state client 생성
    state = client.download_state()  # state 다운로드
    print(state)  # state 출력(확인용)
