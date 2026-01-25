from __future__ import annotations  # 타입 힌트 안정화
import subprocess  # git 커맨드 실행
from dataclasses import dataclass  # 간단 구조
from pathlib import Path  # 경로 처리
from typing import Any, Dict, List, Optional  # 타입 힌트


@dataclass
class GitPublisher:  # git add/commit/push 담당
    repo_dir: Path  # git repo 루트 경로
    branch: str = "main"  # 기본 브랜치

    def _run(self, args: List[str]) -> str:  # git 명령 실행 공통 함수
        result = subprocess.run(  # subprocess 실행
            args,  # 실행할 커맨드 리스트
            cwd=str(self.repo_dir),  # 작업 디렉토리를 repo 루트로 고정
            capture_output=True,  # stdout/stderr 캡처
            text=True,  # 결과를 문자열로 받기
            encoding="utf-8",  # 인코딩
        )
        if result.returncode != 0:  # 실패하면
            raise RuntimeError(f"Command failed: {' '.join(args)}\nSTDERR:\n{result.stderr}")  # 에러 출력
        return result.stdout.strip()  # 성공 stdout 반환

    def ensure_git_available(self) -> None:  # git 사용 가능 여부 체크
        self._run(["git", "--version"])  # git 버전 호출되면 설치/경로 OK

    def ensure_repo(self) -> None:  # 현재 폴더가 git repo인지 확인
        self._run(["git", "rev-parse", "--is-inside-work-tree"])  # git repo면 true 반환

    def has_changes(self) -> bool:  # 커밋할 변경사항이 있는지 확인
        out = self._run(["git", "status", "--porcelain"])  # 변경사항을 간단 포맷으로 출력
        return bool(out)  # 비어있지 않으면 변경 있음

    def add_all(self) -> None:  # 변경 파일 전부 stage
        
        # 수정 후: 루트의 모든 변경 사항(posts.json 포함)을 stage에 올림
        self._run(["git", "add", "."])

    def commit(self, message: str) -> None:  # 커밋 수행
        # 커밋할 것이 없으면 커밋 명령이 실패하므로 사전 체크
        if not self.has_changes():  # 변경 없으면
            return  # 그냥 종료
        self._run(["git", "commit", "-m", message])  # 커밋 실행

    def push(self) -> None:  # push 수행
        self._run(["git", "push", "origin", self.branch])  # origin 브랜치로 push

    def publish(self, commit_message: str) -> None:  # add + commit + push 한번에
        self.ensure_git_available()  # git 확인
        self.ensure_repo()  # repo 확인
        if not self.has_changes():  # 변경 없으면
            print("No changes to publish.")  # 안내
            return  # 종료
        self.add_all()  # stage
        self.commit(commit_message)  # commit
        self.push()  # push


def create_git_publisher(config: Dict[str, Any]) -> GitPublisher:  # config로 GitPublisher 생성
    base_dir = Path(__file__).resolve().parent.parent  # 프로젝트 루트
    git_cfg = config.get("git", {})  # git 섹션
    branch = git_cfg.get("branch", "main")  # 브랜치 기본 main
    return GitPublisher(repo_dir=base_dir, branch=branch)  # 객체 생성


if __name__ == "__main__":  # 단독 테스트(변경사항 있으면 커밋+푸시)
    from app.config_loader import load_config  # 설정 로드

    cfg = load_config()  # config 로드
    gp = create_git_publisher(cfg)  # git publisher 생성
    gp.publish("chore: publish new blog post (pipeline)")  # 커밋+푸시 실행
