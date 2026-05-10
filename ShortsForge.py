import os
import re
import io
import math
import json
import uuid
import textwrap
import asyncio
import tempfile
import urllib.parse
from pathlib import Path
from datetime import datetime

import feedparser
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
)

# =========================
# 기본 설정
# =========================

st.set_page_config(
    page_title="ShortsForge MVP",
    page_icon="🎬",
    layout="wide",
)

APP_TITLE = "🎬 ShortsForge MVP"
RSS_URL = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"

VOICE_OPTIONS = [
    ("ko-KR-SunHiNeural", "여성 1 - SunHi"),
    ("ko-KR-InJoonNeural", "남성 1 - InJoon"),
    ("ko-KR-HyunsuNeural", "남성 2 - Hyunsu"),
    ("ko-KR-JiMinNeural", "여성 2 - JiMin"),
    ("ko-KR-BongJinNeural", "남성 3 - BongJin"),
]

TARGET_W = 1080
TARGET_H = 1920

# =========================
# 세션 상태 초기화
# =========================

def init_session():
    defaults = {
        "run_id": uuid.uuid4().hex[:8],
        "topics": [],
        "selected_topic": None,
        "storyboards": [],
        "selected_storyboard_idx": None,
        "scenes": [],
        "script_text": "",
        "subtitles": [],
        "voice_previews": [],
        "selected_voice_path": None,
        "selected_voice_name": None,
        "final_video_path": None,
        "final_srt_path": None,
        "topic_search_done": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def get_work_dir() -> Path:
    root = Path(".shortsforge_workspace")
    root.mkdir(exist_ok=True)
    run_dir = root / st.session_state["run_id"]
    run_dir.mkdir(exist_ok=True)
    (run_dir / "images").mkdir(exist_ok=True)
    (run_dir / "audio").mkdir(exist_ok=True)
    (run_dir / "subtitle_images").mkdir(exist_ok=True)
    (run_dir / "output").mkdir(exist_ok=True)
    return run_dir


# =========================
# 유틸
# =========================

def clean_title(title: str) -> str:
    title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()
    title = re.sub(r"\[[^\]]+\]", "", title).strip()
    title = re.sub(r"\([^)]*\)$", "", title).strip()
    return title


def normalize_key(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", text).lower()


def wrap_korean_text(text: str, max_chars: int = 16):
    if not text:
        return []
    words = text.split()
    if len(words) == 1:
        # 공백이 거의 없으면 글자 단위로 끊기
        result = []
        line = ""
        for ch in text:
            if len(line) >= max_chars:
                result.append(line)
                line = ch
            else:
                line += ch
        if line:
            result.append(line)
        return result

    lines = []
    current = ""
    for w in words:
        tentative = (current + " " + w).strip()
        if len(tentative) <= max_chars:
            current = tentative
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def estimate_duration(text: str) -> float:
    # 한국어 기준 대략적인 읽기 시간
    chars = len(re.sub(r"\s+", "", text))
    base = max(2.2, chars / 6.0)
    return min(base, 7.5)


def normalize_durations(durations, target_total=34.0):
    if not durations:
        return durations
    current_total = sum(durations)
    if current_total <= 0:
        return durations
    scale = target_total / current_total
    out = [max(2.0, round(d * scale, 2)) for d in durations]
    # 마지막 보정
    diff = round(target_total - sum(out), 2)
    if out:
        out[-1] = round(out[-1] + diff, 2)
    return out


def find_korean_font():
    candidates = [
        "C:/Windows/Fonts/malgunbd.ttf",
        "C:/Windows/Fonts/malgun.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_font(size=48, bold=False):
    font_path = find_korean_font()
    if font_path:
        try:
            return ImageFont.truetype(font_path, size=size)
        except:
            pass
    return ImageFont.load_default()


def safe_filename(text: str):
    text = re.sub(r"[^\w가-힣\-]+", "_", text.strip())
    return text[:50] if text else "output"


# =========================
# 1. 최근 이슈 주제 추천
# =========================

def fetch_trending_topics(limit=5):
    topics = []
    used = set()

    try:
        feed = feedparser.parse(RSS_URL)
        for entry in feed.entries:
            title = clean_title(entry.title)
            if len(title) < 6:
                continue
            key = normalize_key(title)
            if key in used:
                continue
            used.add(key)
            topics.append({
                "title": title,
                "link": getattr(entry, "link", ""),
                "summary": getattr(entry, "summary", ""),
            })
            if len(topics) >= limit:
                break
    except Exception:
        pass

    if not topics:
        # 오프라인/실패 시 fallback
        fallback = [
            "국내 증시 변동성과 투자심리",
            "AI 신기술과 일자리 변화",
            "다이어트와 건강식단 트렌드",
            "전기차 시장 경쟁과 소비자 관심",
            "대중문화 속 화제의 콘텐츠 분석",
        ]
        topics = [{"title": t, "link": "", "summary": ""} for t in fallback[:limit]]

    return topics[:limit]


# =========================
# 2. 스토리보드 5개 생성 (템플릿 기반)
# =========================

def build_storyboard_templates(topic: str):
    templates = []

    templates.append({
        "name": "후킹형",
        "description": "강한 첫 문장으로 시작해 핵심 이유와 포인트를 빠르게 전달하는 구조",
        "scenes": [
            f"{topic}, 요즘 왜 이렇게 주목받는지 알고 계신가요?",
            f"지금 사람들의 관심이 몰리는 가장 큰 이유를 먼저 짚어보겠습니다.",
            f"{topic}가 화제가 된 배경에는 생각보다 분명한 흐름이 있습니다.",
            f"이 이슈를 볼 때 꼭 알아야 할 핵심 포인트만 짧고 쉽게 정리해드립니다.",
            "마지막으로 이 흐름이 앞으로 어떤 의미가 있는지 한 문장으로 정리합니다.",
        ]
    })

    templates.append({
        "name": "문제-원인-해결형",
        "description": "왜 문제가 생겼는지, 왜 관심이 커졌는지, 어떻게 해석해야 하는지 설명하는 구조",
        "scenes": [
            f"{topic}를 둘러싼 관심이 갑자기 커진 이유부터 보겠습니다.",
            f"많은 사람들이 궁금해하는 핵심 문제는 바로 이것입니다.",
            f"이런 관심이 커진 원인은 최근 흐름과 연결되어 있습니다.",
            f"그래서 우리는 이 이슈를 이렇게 해석하면 됩니다.",
            "정리하면, 지금 꼭 챙겨봐야 할 포인트는 이 한 가지입니다.",
        ]
    })

    templates.append({
        "name": "5포인트 요약형",
        "description": "짧은 정보형 쇼츠에 적합한 5단계 요약 구조",
        "scenes": [
            f"첫째, {topic}가 왜 화제인지 한 줄로 정리합니다.",
            "둘째, 사람들이 특히 반응하는 포인트를 짚어봅니다.",
            "셋째, 이 이슈가 실제로 중요한 이유를 설명합니다.",
            "넷째, 놓치기 쉬운 핵심 포인트를 짧게 추가합니다.",
            "다섯째, 지금 시점에서 우리가 기억할 결론을 정리합니다.",
        ]
    })

    templates.append({
        "name": "팩트체크형",
        "description": "오해와 진실을 구분하는 설명형 스토리 구조",
        "scenes": [
            f"{topic}에 대해 사람들이 가장 많이 오해하는 부분부터 시작합니다.",
            "겉으로 보이는 현상과 실제 핵심은 다를 수 있습니다.",
            "그래서 지금 꼭 구분해야 할 사실을 짚어보겠습니다.",
            "이 이슈를 제대로 이해하려면 이 포인트를 놓치면 안 됩니다.",
            "결론적으로, 지금 가장 중요한 해석만 간단히 정리합니다.",
        ]
    })

    templates.append({
        "name": "스토리텔링형",
        "description": "짧은 도입-전개-결론 구조로 몰입감 있게 전달하는 방식",
        "scenes": [
            f"처음에는 단순한 관심처럼 보였지만, {topic}는 빠르게 커졌습니다.",
            "사람들의 반응이 커지면서 이 이슈는 하나의 흐름이 되었습니다.",
            "그 흐름이 만들어진 배경에는 분명한 이유가 있습니다.",
            "그래서 지금 이 주제를 보는 시선도 달라지고 있습니다.",
            "결국 이 이슈가 남기는 핵심 메시지는 이것입니다.",
        ]
    })

    storyboards = []
    for idx, t in enumerate(templates, start=1):
        scenes = []
        for scene_idx, scene_text in enumerate(t["scenes"], start=1):
            prompt = (
                f"Vertical 9:16 cinematic social media illustration, no text, no watermark. "
                f"Topic: {topic}. Scene meaning: {scene_text}. "
                f"Modern Korean YouTube shorts visual, attention-grabbing, high detail, vibrant, clean composition."
            )
            scenes.append({
                "scene_no": scene_idx,
                "narration": scene_text,
                "image_prompt": prompt,
                "image_path": None,
                "duration": 0.0,
                "start": 0.0,
                "end": 0.0,
                "seed": scene_idx * 100 + idx,
            })

        storyboards.append({
            "idx": idx,
            "name": t["name"],
            "description": t["description"],
            "topic": topic,
            "scenes": scenes,
        })
    return storyboards


# =========================
# 3. 이미지 생성
# =========================

def create_fallback_vertical_image(text: str, out_path: Path, topic: str = ""):
    img = Image.new("RGB", (TARGET_W, TARGET_H), color=(20, 24, 34))
    draw = ImageDraw.Draw(img)

    # 부드러운 배경 장식
    overlay = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse((100, 150, 900, 900), fill=(80, 120, 255, 90))
    od.ellipse((200, 1000, 1000, 1750), fill=(255, 120, 120, 70))
    overlay = overlay.filter(ImageFilter.GaussianBlur(80))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    font_big = load_font(70)
    font_mid = load_font(42)
    font_small = load_font(30)

    title_lines = wrap_korean_text(topic or "ShortsForge", max_chars=12)
    scene_lines = wrap_korean_text(text, max_chars=16)

    y = 180
    draw.rounded_rectangle((90, 120, 990, 400), radius=30, fill=(0, 0, 0, 120))
    draw.text((120, 150), "SCENE PREVIEW", font=font_small, fill=(255, 220, 120))

    for line in title_lines[:2]:
        draw.text((120, y + 60), line, font=font_big, fill=(255, 255, 255))
        y += 78

    draw.rounded_rectangle((80, 520, 1000, 1450), radius=36, fill=(0, 0, 0, 130))
    yy = 620
    for line in scene_lines[:8]:
        draw.text((120, yy), line, font=font_mid, fill=(230, 235, 245))
        yy += 60

    draw.text((120, 1560), "무료 이미지 생성 실패 시 자동 대체 카드", font=font_small, fill=(210, 210, 210))
    draw.text((120, 1620), "나중에 API 연결하면 더 고급 이미지로 업그레이드 가능", font=font_small, fill=(210, 210, 210))

    img.save(out_path)


def try_generate_image_pollinations(prompt: str, out_path: Path, seed: int):
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width={TARGET_W}&height={TARGET_H}&seed={seed}&nologo=true"
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    out_path.write_bytes(resp.content)
    return out_path


def generate_scene_image(scene: dict, topic: str, image_dir: Path):
    scene_no = scene["scene_no"]
    seed = scene.get("seed", scene_no * 100)
    filename = f"scene_{scene_no}_{seed}.png"
    out_path = image_dir / filename

    try:
        try_generate_image_pollinations(scene["image_prompt"], out_path, seed)
    except Exception:
        create_fallback_vertical_image(scene["narration"], out_path, topic=topic)

    scene["image_path"] = str(out_path)
    return scene


# =========================
# 4. 대본/자막 생성
# =========================

def generate_script_and_subtitles(scenes):
    texts = [s["narration"].strip() for s in scenes]
    durations = [estimate_duration(t) for t in texts]
    durations = normalize_durations(durations, target_total=34.0)

    subtitles = []
    current = 0.0
    full_script = []

    for i, (scene, dur) in enumerate(zip(scenes, durations), start=1):
        start = round(current, 2)
        end = round(current + dur, 2)

        scene["duration"] = dur
        scene["start"] = start
        scene["end"] = end

        subtitle_text = scene["narration"].strip()
        subtitles.append({
            "index": i,
            "start": start,
            "end": end,
            "text": subtitle_text
        })
        full_script.append(subtitle_text)
        current += dur

    script_text = "\n\n".join(full_script)
    return script_text, subtitles, scenes


def sec_to_srt_time(sec: float) -> str:
    total_ms = int(sec * 1000)
    hours = total_ms // 3600000
    total_ms %= 3600000
    minutes = total_ms // 60000
    total_ms %= 60000
    seconds = total_ms // 1000
    ms = total_ms % 1000
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def write_srt(subtitles, out_path: Path):
    lines = []
    for item in subtitles:
        lines.append(str(item["index"]))
        lines.append(f"{sec_to_srt_time(item['start'])} --> {sec_to_srt_time(item['end'])}")
        lines.append(item["text"])
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# =========================
# 5. 음성 생성
# =========================

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def save_tts_async(text: str, voice_id: str, out_path: str, rate: str = "+0%"):
    import edge_tts
    communicate = edge_tts.Communicate(text=text, voice=voice_id, rate=rate)
    await communicate.save(out_path)


def generate_tts_file(text: str, voice_id: str, out_path: Path, rate="+0%"):
    run_async(save_tts_async(text, voice_id, str(out_path), rate=rate))
    return out_path


# =========================
# 6. 자막 이미지 만들기
# =========================

def create_subtitle_card(text: str, out_path: Path):
    img = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = load_font(56)
    font_small = load_font(48)

    lines = wrap_korean_text(text, max_chars=15)
    if len(lines) > 4:
        lines = lines[:4]

    line_height = 72
    box_padding_x = 40
    box_padding_y = 28
    total_text_h = len(lines) * line_height

    y_start = TARGET_H - 420 - total_text_h
    if y_start < 200:
        y_start = 220

    max_w = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        max_w = max(max_w, line_w)

    box_w = max_w + box_padding_x * 2
    box_h = total_text_h + box_padding_y * 2
    x1 = (TARGET_W - box_w) // 2
    y1 = y_start
    x2 = x1 + box_w
    y2 = y1 + box_h

    shadow = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((x1 + 6, y1 + 8, x2 + 6, y2 + 8), radius=28, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(img, shadow)

    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=28, fill=(10, 10, 10, 165), outline=(255, 255, 255, 40), width=2)

    yy = y1 + box_padding_y
    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        xx = (TARGET_W - line_w) // 2
        # 그림자
        draw.text((xx + 2, yy + 2), line, font=font, fill=(0, 0, 0, 180))
        # 본문
        color = (255, 255, 255, 255)
        if idx == 0:
            color = (255, 230, 120, 255)
        draw.text((xx, yy), line, font=font, fill=color)
        yy += line_height

    img.save(out_path)


# =========================
# 7. 영상 렌더링
# =========================

def build_background_clip(image_path: str, duration: float):
    img = Image.open(image_path)
    w, h = img.size
    scale = max(TARGET_W / w, TARGET_H / h)
    new_w = int(w * scale)
    new_h = int(h * scale)

    clip = ImageClip(image_path).resize((new_w, new_h))
    x1 = max(0, int((new_w - TARGET_W) / 2))
    y1 = max(0, int((new_h - TARGET_H) / 2))
    clip = clip.crop(x1=x1, y1=y1, x2=x1 + TARGET_W, y2=y1 + TARGET_H)
    clip = clip.set_duration(duration)
    return clip


def render_final_video(scenes, subtitles, audio_path: str, output_path: Path, subtitle_img_dir: Path):
    clips = []

    for idx, scene in enumerate(scenes, start=1):
        bg = build_background_clip(scene["image_path"], scene["duration"])

        sub_item = subtitles[idx - 1]
        sub_img_path = subtitle_img_dir / f"sub_{idx}.png"
        create_subtitle_card(sub_item["text"], sub_img_path)

        sub_clip = (
            ImageClip(str(sub_img_path))
            .set_duration(scene["duration"])
            .set_position(("center", "center"))
        )

        composed = CompositeVideoClip([bg, sub_clip], size=(TARGET_W, TARGET_H))
        clips.append(composed)

    final = concatenate_videoclips(clips, method="compose")

    if audio_path and os.path.exists(audio_path):
        audio_clip = AudioFileClip(audio_path)
        final = final.set_audio(audio_clip)

    final.write_videofile(
        str(output_path),
        fps=30,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=str(output_path.parent / "temp_audio.m4a"),
        remove_temp=True,
        threads=4,
        preset="medium",
    )

    final.close()
    return output_path


# =========================
# UI
# =========================

init_session()
work_dir = get_work_dir()

st.title(APP_TITLE)
st.caption("검색 → 주제 선택 → 스토리보드 선택 → 이미지 생성/컨펌 → 대본/자막 생성 → 음성 미리듣기 → 최종 렌더")

with st.expander("사용 전 안내", expanded=False):
    st.markdown(
        """
- 이 버전은 **무료 MVP**입니다.
- **주제 추천**은 Google News RSS 기반입니다.
- **스토리보드/대본**은 현재 **템플릿 기반 자동 생성**입니다.
- **이미지 생성**은 무료 엔드포인트를 우선 사용하고, 실패 시 자동 대체 이미지로 바뀝니다.
- **음성 생성**은 `edge-tts` 기반입니다.
- **최종 렌더링**은 PC 사양에 따라 시간이 걸릴 수 있습니다.
        """
    )

# -------------------------
# 1) 최근 이슈 검색
# -------------------------
st.subheader("1) 최근 이슈 주제 추천")

col_a, col_b = st.columns([1, 1])

with col_a:
    if st.button("🔎 최근 이슈 주제 5개 추천", use_container_width=True):
        with st.spinner("최근 이슈를 가져오는 중입니다..."):
            st.session_state["topics"] = fetch_trending_topics(limit=5)
            st.session_state["selected_topic"] = None
            st.session_state["storyboards"] = []
            st.session_state["selected_storyboard_idx"] = None
            st.session_state["scenes"] = []
            st.session_state["script_text"] = ""
            st.session_state["subtitles"] = []
            st.session_state["voice_previews"] = []
            st.session_state["selected_voice_path"] = None
            st.session_state["selected_voice_name"] = None
            st.session_state["final_video_path"] = None
            st.session_state["final_srt_path"] = None
            st.session_state["topic_search_done"] = True

with col_b:
    manual_topic = st.text_input("또는 직접 주제 입력", value="")

if st.session_state["topics"]:
    topic_titles = [t["title"] for t in st.session_state["topics"]]
    default_index = 0
    selected_topic = st.radio(
        "추천 주제 중 선택",
        topic_titles,
        index=default_index,
        horizontal=False,
    )
    st.session_state["selected_topic"] = selected_topic

if manual_topic.strip():
    st.session_state["selected_topic"] = manual_topic.strip()
    st.info(f"직접 입력 주제 사용: {manual_topic.strip()}")

if st.session_state["selected_topic"]:
    st.success(f"선택된 주제: {st.session_state['selected_topic']}")

    if st.button("🧩 선택 주제로 스토리보드 5개 생성", use_container_width=True):
        with st.spinner("스토리보드를 생성하는 중입니다..."):
            st.session_state["storyboards"] = build_storyboard_templates(st.session_state["selected_topic"])
            st.session_state["selected_storyboard_idx"] = None
            st.session_state["scenes"] = []
            st.session_state["script_text"] = ""
            st.session_state["subtitles"] = []
            st.session_state["voice_previews"] = []
            st.session_state["selected_voice_path"] = None
            st.session_state["selected_voice_name"] = None
            st.session_state["final_video_path"] = None
            st.session_state["final_srt_path"] = None

# -------------------------
# 2) 스토리보드 선택
# -------------------------
if st.session_state["storyboards"]:
    st.subheader("2) 스토리보드 5개 중 선택")

    sb_options = [
        f"{sb['idx']}. {sb['name']} - {sb['description']}"
        for sb in st.session_state["storyboards"]
    ]
    selected_sb_label = st.radio("스토리보드 선택", sb_options, index=0)

    chosen_idx = int(selected_sb_label.split(".")[0])
    st.session_state["selected_storyboard_idx"] = chosen_idx - 1
    selected_sb = st.session_state["storyboards"][st.session_state["selected_storyboard_idx"]]

    with st.expander("선택한 스토리보드 상세 보기", expanded=True):
        st.markdown(f"**스토리보드명:** {selected_sb['name']}")
        st.markdown(f"**설명:** {selected_sb['description']}")
        st.markdown("**씬 구성:**")
        for scene in selected_sb["scenes"]:
            st.write(f"- Scene {scene['scene_no']}: {scene['narration']}")

    if st.button("🖼️ 이 스토리보드로 씬 이미지 생성", use_container_width=True):
        with st.spinner("씬 이미지를 생성하는 중입니다..."):
            topic = selected_sb["topic"]
            image_dir = work_dir / "images"
            generated_scenes = []
            for scene in selected_sb["scenes"]:
                generated = generate_scene_image(scene.copy(), topic, image_dir)
                generated_scenes.append(generated)
            st.session_state["scenes"] = generated_scenes
            st.session_state["script_text"] = ""
            st.session_state["subtitles"] = []
            st.session_state["voice_previews"] = []
            st.session_state["selected_voice_path"] = None
            st.session_state["selected_voice_name"] = None
            st.session_state["final_video_path"] = None
            st.session_state["final_srt_path"] = None

# -------------------------
# 3) 씬 이미지 컨펌 / 개별 재생성
# -------------------------
if st.session_state["scenes"]:
    st.subheader("3) 씬 이미지 확인 / 개별 재생성")

    for idx, scene in enumerate(st.session_state["scenes"]):
        with st.container(border=True):
            col1, col2 = st.columns([1, 1])

            with col1:
                st.markdown(f"### Scene {scene['scene_no']}")
                if scene["image_path"] and os.path.exists(scene["image_path"]):
                    st.image(scene["image_path"], use_container_width=True)
                else:
                    st.warning("이미지가 아직 없습니다.")

            with col2:
                st.markdown("**내레이션 / 자막 문장**")
                new_narration = st.text_area(
                    f"Scene {scene['scene_no']} 문장 수정",
                    value=scene["narration"],
                    height=120,
                    key=f"narration_{scene['scene_no']}",
                )
                scene["narration"] = new_narration

                new_prompt = st.text_area(
                    f"Scene {scene['scene_no']} 이미지 프롬프트 수정",
                    value=scene["image_prompt"],
                    height=160,
                    key=f"prompt_{scene['scene_no']}",
                )
                scene["image_prompt"] = new_prompt

                if st.button(f"♻️ Scene {scene['scene_no']} 이미지만 다시 생성", key=f"regen_{scene['scene_no']}"):
                    with st.spinner(f"Scene {scene['scene_no']} 이미지 재생성 중..."):
                        scene["seed"] = int(scene.get("seed", 0)) + 1
                        updated = generate_scene_image(
                            scene,
                            st.session_state["selected_topic"],
                            work_dir / "images"
                        )
                        st.session_state["scenes"][idx] = updated
                    st.rerun()

    if st.button("✅ 이미지/스토리보드 확정 → 대본/자막 생성", use_container_width=True):
        with st.spinner("대본과 자막을 생성하는 중입니다..."):
            script_text, subtitles, scenes = generate_script_and_subtitles(st.session_state["scenes"])
            st.session_state["script_text"] = script_text
            st.session_state["subtitles"] = subtitles
            st.session_state["scenes"] = scenes
            st.session_state["voice_previews"] = []
            st.session_state["selected_voice_path"] = None
            st.session_state["selected_voice_name"] = None
            st.session_state["final_video_path"] = None
            st.session_state["final_srt_path"] = None

# -------------------------
# 4) 대본 / 자막 확인
# -------------------------
if st.session_state["script_text"] and st.session_state["subtitles"]:
    st.subheader("4) 생성된 대본 / 자막 확인")

    st.markdown("### 전체 대본")
    st.text_area("대본", value=st.session_state["script_text"], height=260)

    st.markdown("### 자막 타임라인")
    for sub in st.session_state["subtitles"]:
        st.write(f"**[{sub['index']}] {sub['start']:.2f}s ~ {sub['end']:.2f}s**  \n{sub['text']}")

    if st.button("🔊 컨펌 음성 생성 (무료 5개 미리듣기)", use_container_width=True):
        with st.spinner("5개 음성 미리듣기를 생성하는 중입니다..."):
            previews = []
            script_for_voice = st.session_state["script_text"]
            audio_dir = work_dir / "audio"

            for voice_id, voice_label in VOICE_OPTIONS:
                out_path = audio_dir / f"{voice_id}.mp3"
                try:
                    generate_tts_file(script_for_voice, voice_id, out_path, rate="+5%")
                    previews.append({
                        "voice_id": voice_id,
                        "voice_label": voice_label,
                        "path": str(out_path)
                    })
                except Exception as e:
                    previews.append({
                        "voice_id": voice_id,
                        "voice_label": voice_label,
                        "path": None,
                        "error": str(e),
                    })

            st.session_state["voice_previews"] = previews
            st.session_state["selected_voice_path"] = None
            st.session_state["selected_voice_name"] = None
            st.session_state["final_video_path"] = None
            st.session_state["final_srt_path"] = None

# -------------------------
# 5) 음성 미리듣기 / 선택
# -------------------------
if st.session_state["voice_previews"]:
    st.subheader("5) 음성 미리듣기 후 선택")

    voice_labels = []
    valid_map = {}
    for item in st.session_state["voice_previews"]:
        if item.get("path") and os.path.exists(item["path"]):
            label = item["voice_label"]
            voice_labels.append(label)
            valid_map[label] = item

    if not voice_labels:
        st.error("사용 가능한 음성 미리듣기 생성에 실패했습니다.")
    else:
        cols = st.columns(min(5, len(voice_labels)))
        for i, label in enumerate(voice_labels):
            item = valid_map[label]
            with cols[i % len(cols)]:
                st.markdown(f"**{label}**")
                st.audio(item["path"], format="audio/mp3")

        chosen_label = st.radio("최종 사용할 음성 선택", voice_labels, horizontal=False)
        chosen_item = valid_map[chosen_label]
        st.session_state["selected_voice_path"] = chosen_item["path"]
        st.session_state["selected_voice_name"] = chosen_item["voice_label"]

        st.success(f"선택된 음성: {chosen_label}")
        st.info("선택이 끝났습니다. 이제 최종 컨펌 버튼으로 영상 렌더링이 가능합니다.")

# -------------------------
# 6) 최종 렌더링
# -------------------------
st.subheader("6) 최종 컨펌 및 다운로드")

can_render = (
    bool(st.session_state["scenes"])
    and bool(st.session_state["subtitles"])
    and bool(st.session_state["selected_voice_path"])
)

render_button = st.button(
    "🎞️ 최종 컨펌 → 영상 렌더링 시작",
    use_container_width=True,
    disabled=not can_render,
)

if render_button:
    with st.spinner("최종 영상을 렌더링하는 중입니다... 시간이 조금 걸릴 수 있습니다."):
        title_stub = safe_filename(st.session_state["selected_topic"] or "shorts")
        output_dir = work_dir / "output"
        video_path = output_dir / f"{title_stub}.mp4"
        srt_path = output_dir / f"{title_stub}.srt"

        write_srt(st.session_state["subtitles"], srt_path)
        render_final_video(
            scenes=st.session_state["scenes"],
            subtitles=st.session_state["subtitles"],
            audio_path=st.session_state["selected_voice_path"],
            output_path=video_path,
            subtitle_img_dir=work_dir / "subtitle_images",
        )

        st.session_state["final_video_path"] = str(video_path)
        st.session_state["final_srt_path"] = str(srt_path)

if st.session_state["final_video_path"] and os.path.exists(st.session_state["final_video_path"]):
    st.success("최종 영상 제작 완료")
    st.video(st.session_state["final_video_path"])

    with open(st.session_state["final_video_path"], "rb") as f:
        st.download_button(
            "📥 최종 MP4 다운로드",
            data=f,
            file_name=os.path.basename(st.session_state["final_video_path"]),
            mime="video/mp4",
            use_container_width=True,
        )

    if st.session_state["final_srt_path"] and os.path.exists(st.session_state["final_srt_path"]):
        with open(st.session_state["final_srt_path"], "rb") as f:
            st.download_button(
                "📥 SRT 자막 다운로드",
                data=f,
                file_name=os.path.basename(st.session_state["final_srt_path"]),
                mime="text/plain",
                use_container_width=True,
            )