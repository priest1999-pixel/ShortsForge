import streamlit as st
import tempfile
import os
import re
import io
import math
import uuid
import textwrap
import asyncio
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from moviepy.editor import (
    AudioFileClip,
    CompositeVideoClip,
    ImageClip,
    concatenate_videoclips,
    concatenate_audioclips,
)
import edge_tts


# ============================================================
# 기본 설정
# ============================================================
st.set_page_config(page_title="ShortsForge - 수동 제작 모드", page_icon="🎬", layout="wide")

VIDEO_W = 1080
VIDEO_H = 1920
SAFE_MARGIN_X = 70
SAFE_MARGIN_BOTTOM = 140
SUBTITLE_BOX_PAD = 28
DEFAULT_FPS = 24

st.markdown(
    """
    <style>
    .sf-card{padding:14px 16px;border:1px solid #2b3547;border-radius:16px;background:#111827;margin-bottom:12px}
    .sf-small{color:#9ca3af;font-size:13px}
    .sf-good{color:#86efac;font-weight:700}
    .sf-warn{color:#fca5a5;font-weight:700}
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# 유틸
# ============================================================
def slugify_filename(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9가-힣_-]+", "_", str(name))
    return name.strip("_") or "shortsforge"


def parse_scene_scripts(raw_text: str) -> List[Dict]:
    """
    Scene 1: ...
    Scene 2: ...
    형식 파싱. 줄바꿈 여러 줄도 허용.
    """
    text = (raw_text or "").strip()
    if not text:
        return []

    pattern = re.compile(
        r"(?:^|\n)\s*(?:Scene|씬)\s*(\d+)\s*[:：\-]\s*(.*?)(?=(?:\n\s*(?:Scene|씬)\s*\d+\s*[:：\-])|$)",
        re.IGNORECASE | re.DOTALL,
    )
    scenes = []
    for m in pattern.finditer(text):
        no = int(m.group(1))
        script = re.sub(r"\s+", " ", m.group(2)).strip()
        if script:
            scenes.append({"scene_no": no, "script": script})

    scenes.sort(key=lambda x: x["scene_no"])
    return scenes


def parse_number_from_filename(filename: str) -> Optional[int]:
    base = Path(filename).stem.lower()
    patterns = [
        r"scene[_\- ]?(\d+)",
        r"씬[_\- ]?(\d+)",
        r"^(\d+)$",
        r"[_\- ](\d+)$",
        r"^(\d+)[_\- ]",
    ]
    for p in patterns:
        m = re.search(p, base)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    nums = re.findall(r"(\d+)", base)
    if nums:
        try:
            return int(nums[-1])
        except Exception:
            return None
    return None


def auto_match_images(scenes: List[Dict], uploaded_files: List) -> List[Dict]:
    """파일명 번호 우선, 없으면 업로드 순서로 매칭."""
    matched = []
    remaining = list(uploaded_files or [])
    by_num = {}
    for f in remaining:
        n = parse_number_from_filename(getattr(f, "name", ""))
        if n is not None and n not in by_num:
            by_num[n] = f

    used_ids = set()
    for idx, scene in enumerate(scenes):
        scene_no = scene["scene_no"]
        f = by_num.get(scene_no)
        if f is None:
            # 업로드 순서 중 아직 안쓴 것 할당
            for cand in remaining:
                cid = id(cand)
                if cid not in used_ids:
                    f = cand
                    break
        if f is not None:
            used_ids.add(id(f))
        matched.append({**scene, "image_file": f})
    return matched


def find_korean_font() -> Optional[str]:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.otf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/malgun.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_font(size: int) -> ImageFont.FreeTypeFont:
    path = find_korean_font()
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def split_subtitle_lines(text: str, max_chars: int = 18, max_lines: int = 3) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if len(test) <= max_chars or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
        if len(lines) >= max_lines - 1:
            # 마지막 줄은 남은 내용 몰아넣기
            continue
    if cur:
        lines.append(cur)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > max_chars - 1:
            lines[-1] = lines[-1][: max_chars - 1] + "…"
    return "\n".join(lines[:max_lines])


def measure_multiline(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    lines = text.splitlines() or [text]
    max_w = 0
    line_hs = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        max_w = max(max_w, w)
        line_hs.append(h)
    total_h = sum(line_hs) + (len(lines) - 1) * 12
    return max_w, total_h


def render_subtitle_image(text: str, width: int = VIDEO_W, height: int = VIDEO_H) -> Image.Image:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    subtitle = split_subtitle_lines(text, max_chars=16, max_lines=3)
    font = load_font(62)
    max_text_w = width - SAFE_MARGIN_X * 2 - SUBTITLE_BOX_PAD * 2

    # 간단한 폰트 크기 보정
    for size in [62, 58, 54, 50, 46, 42, 38]:
        font = load_font(size)
        tw, th = measure_multiline(draw, subtitle, font)
        if tw <= max_text_w:
            break

    tw, th = measure_multiline(draw, subtitle, font)
    box_w = min(max_text_w + SUBTITLE_BOX_PAD * 2, tw + SUBTITLE_BOX_PAD * 2)
    box_h = th + SUBTITLE_BOX_PAD * 2
    x1 = (width - box_w) // 2
    y1 = height - SAFE_MARGIN_BOTTOM - box_h
    x2 = x1 + box_w
    y2 = y1 + box_h

    # 반투명 박스
    draw.rounded_rectangle((x1, y1, x2, y2), radius=28, fill=(0, 0, 0, 175))

    # 텍스트
    lines = subtitle.splitlines() or [subtitle]
    current_y = y1 + SUBTITLE_BOX_PAD
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        tx = (width - lw) // 2
        draw.text(
            (tx, current_y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 220),
        )
        current_y += lh + 12
    return canvas


def open_image_from_upload(uploaded_file) -> Image.Image:
    return Image.open(uploaded_file).convert("RGB")


def make_vertical_background(img: Image.Image, size=(VIDEO_W, VIDEO_H)) -> Image.Image:
    target_w, target_h = size
    bg = img.resize(size, Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=24))
    # 어둡게 살짝
    overlay = Image.new("RGBA", size, (0, 0, 0, 70))
    bg = bg.convert("RGBA")
    bg.alpha_composite(overlay)
    return bg.convert("RGB")


def fit_foreground_on_vertical(img: Image.Image, size=(VIDEO_W, VIDEO_H)) -> Image.Image:
    target_w, target_h = size
    bg = make_vertical_background(img, size=size).convert("RGBA")
    fg = img.copy().convert("RGBA")
    fg.thumbnail((target_w - 40, target_h - 220), Image.LANCZOS)
    x = (target_w - fg.width) // 2
    y = (target_h - fg.height) // 2 - 40
    bg.alpha_composite(fg, (x, y))
    return bg.convert("RGB")


async def edge_tts_save_async(text: str, out_path: str, voice: str, rate: str = "+0%"):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(out_path)


def edge_tts_save(text: str, out_path: str, voice: str, rate: str = "+0%"):
    asyncio.run(edge_tts_save_async(text, out_path, voice, rate))


def save_uploaded_to_path(uploaded_file, dest_path: str):
    with open(dest_path, "wb") as f:
        f.write(uploaded_file.getbuffer())


def build_scene_assets(scene: Dict, workdir: str, voice: str, rate: str) -> Dict:
    scene_no = scene["scene_no"]
    script = scene["script"]
    uploaded_file = scene.get("image_file")
    if uploaded_file is None:
        raise ValueError(f"Scene {scene_no} 이미지가 없습니다.")

    image = open_image_from_upload(uploaded_file)
    final_bg = fit_foreground_on_vertical(image)

    img_path = os.path.join(workdir, f"scene_{scene_no:02d}.png")
    final_bg.save(img_path, format="PNG")

    subtitle_img = render_subtitle_image(script)
    subtitle_path = os.path.join(workdir, f"scene_{scene_no:02d}_sub.png")
    subtitle_img.save(subtitle_path, format="PNG")

    audio_path = os.path.join(workdir, f"scene_{scene_no:02d}.mp3")
    edge_tts_save(script, audio_path, voice=voice, rate=rate)

    audio_clip = AudioFileClip(audio_path)
    duration = max(audio_clip.duration, 0.1)
    audio_clip.close()

    return {
        **scene,
        "image_path": img_path,
        "subtitle_path": subtitle_path,
        "audio_path": audio_path,
        "duration": duration,
    }


def compose_video(scene_assets: List[Dict], out_path: str, fps: int = DEFAULT_FPS):
    clips = []
    try:
        for item in scene_assets:
            duration = float(item["duration"])
            bg_clip = ImageClip(item["image_path"]).set_duration(duration)
            sub_clip = (
                ImageClip(item["subtitle_path"], transparent=True)
                .set_duration(duration)
                .set_position((0, 0))
            )
            audio_clip = AudioFileClip(item["audio_path"])
            scene_clip = CompositeVideoClip([bg_clip, sub_clip], size=(VIDEO_W, VIDEO_H)).set_audio(audio_clip)
            clips.append(scene_clip)

        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(
            out_path,
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            preset="medium",
            threads=2,
            verbose=False,
            logger=None,
        )
        final.close()
    finally:
        for c in clips:
            try:
                if c.audio:
                    c.audio.close()
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass


# ============================================================
# UI
# ============================================================
st.title("🎬 ShortsForge - 수동 제작 모드")
st.caption("씬 대본 + 씬별 이미지로 자막/음성/쇼츠 영상을 자동 생성합니다.")

with st.expander("사용 방법", expanded=False):
    st.markdown(
        """
        1. **주제**와 **Scene 대본**을 입력합니다.  
        2. **여러 이미지를 업로드**합니다. (`scene1.png`, `scene2.png` 같은 파일명 권장)  
        3. 앱이 **Scene 번호와 이미지**를 자동 매칭합니다.  
        4. **영상 생성**을 누르면 자막+음성+세로형 쇼츠 MP4를 만듭니다.
        """
    )

col1, col2 = st.columns([1.1, 0.9])
with col1:
    topic = st.text_input("주제", value="삼성전자 HBM 이슈")
    script_input = st.text_area(
        "씬 대본 입력",
        height=260,
        value=(
            "Scene 1: 삼성전자가 다시 주목받는 이유는 AI 메모리 수요와 HBM 기대감이 동시에 커졌기 때문입니다.\n"
            "Scene 2: 핵심은 이 기대감이 실제 실적 회복으로 이어질 수 있느냐입니다.\n"
            "Scene 3: 최근 메모리 업황 회복과 외국인 수급 변화가 관심을 키우고 있습니다.\n"
            "Scene 4: 하지만 단기 뉴스보다 실적 발표와 공급 확인이 더 중요합니다.\n"
            "Scene 5: 정리하면 핵심은 기대감이 아니라 실적 확인입니다."
        ),
        help="Scene 1: ... 형식으로 입력하세요.",
    )

    voice = st.selectbox(
        "음성",
        options=[
            "ko-KR-SunHiNeural",
            "ko-KR-InJoonNeural",
            "ko-KR-HyunsuNeural",
            "ko-KR-BongJinNeural",
        ],
        index=0,
    )
    rate = st.select_slider("음성 속도", options=["-20%", "-10%", "+0%", "+10%", "+20%"], value="+0%")

with col2:
    uploaded_files = st.file_uploader(
        "씬 이미지 업로드",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        help="파일명에 scene1, scene2 같은 번호가 있으면 자동 매칭 정확도가 올라갑니다.",
    )

scenes = parse_scene_scripts(script_input)
matched_scenes = auto_match_images(scenes, uploaded_files or [])

st.subheader("1) Scene 파싱 결과")
if not scenes:
    st.warning("Scene 형식의 대본을 찾지 못했습니다. `Scene 1: ...` 형식으로 입력해 주세요.")
else:
    st.success(f"총 {len(scenes)}개 씬을 찾았습니다.")
    for scene in scenes:
        st.markdown(f"- **Scene {scene['scene_no']}**: {scene['script']}")

st.subheader("2) 이미지 자동 매칭")
missing_images = 0
for item in matched_scenes:
    file = item.get("image_file")
    with st.container(border=True):
        c1, c2 = st.columns([0.9, 1.1])
        with c1:
            st.markdown(f"**Scene {item['scene_no']}**")
            st.write(item["script"])
            if file is not None:
                st.markdown(f"<div class='sf-good'>매칭 완료: {file.name}</div>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div class='sf-warn'>매칭된 이미지 없음</div>", unsafe_allow_html=True)
                missing_images += 1
        with c2:
            if file is not None:
                st.image(file, width=180)
            else:
                st.info("이미지를 업로드하면 여기에 미리보기가 표시됩니다.")

if scenes and uploaded_files:
    if len(uploaded_files) < len(scenes):
        st.warning(f"업로드 이미지 수({len(uploaded_files)})가 씬 수({len(scenes)})보다 적습니다.")
    elif len(uploaded_files) > len(scenes):
        st.info(f"업로드 이미지 수({len(uploaded_files)})가 씬 수({len(scenes)})보다 많습니다. 앞쪽부터 우선 사용합니다.")

st.subheader("3) 영상 생성")
can_build = bool(scenes) and missing_images == 0
if not can_build:
    st.info("모든 Scene에 이미지가 매칭되어야 영상을 생성할 수 있습니다.")

if st.button("🎬 쇼츠 영상 생성", type="primary", disabled=not can_build, use_container_width=True):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            progress = st.progress(0)
            status = st.empty()

            scene_assets = []
            total_steps = max(len(matched_scenes) + 1, 1)
            for idx, scene in enumerate(matched_scenes, start=1):
                status.write(f"Scene {scene['scene_no']} 자산 생성 중...")
                asset = build_scene_assets(scene, tmpdir, voice=voice, rate=rate)
                scene_assets.append(asset)
                progress.progress(min(idx / total_steps, 0.95))

            safe_topic = slugify_filename(topic)
            out_path = os.path.join(tmpdir, f"{safe_topic}_shorts.mp4")
            status.write("최종 영상 합성 중...")
            compose_video(scene_assets, out_path, fps=DEFAULT_FPS)
            progress.progress(1.0)
            status.success("영상 생성 완료!")

            with open(out_path, "rb") as f:
                video_bytes = f.read()

            st.video(video_bytes)
            st.download_button(
                "📥 MP4 다운로드",
                data=video_bytes,
                file_name=f"{safe_topic}_shorts.mp4",
                mime="video/mp4",
                use_container_width=True,
            )
    except Exception as e:
        st.error(f"영상 생성 중 오류가 발생했습니다: {e}")
        st.exception(e)
