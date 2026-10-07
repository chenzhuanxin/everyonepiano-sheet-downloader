# -*- coding: utf-8 -*-
"""
人人钢琴曲谱下载器  (everyonepiano.cn)
=====================================
自用工具：慢速、可断点续跑、带面板，Cookie 由使用者自行填写。

【免费 / 需 VIP 的实测结论】
  免费（不需要 VIP，登录与否都能下）：
      · EOP  文件          —— 扩展名 .eop
      · EOPM 文件          —— 扩展名 .eopm（部分老曲目没有）
      · 五线谱大图 PNG     —— 2500×3400 左右，-w-b-N.png
      · 双手简谱大图 PNG   —— 2381×3368 左右，-j-b-N.png
      · 缩略图 JPG         —— 200×260，仅缩略图，默认不下载
  需要 VIP（本工具绝不下载，也不做任何绕过）：
      · MP3 / PDF / EOPN / MIDI   —— 这四个页面只提供「购买VIP会员」按钮

【Cookie 怎么填】
  浏览器登录 gongqizi1 后，按 F12 → Network → 随便点一个请求 → Headers →
  复制 Cookie: 后面那一整串（形如 PHPSESSID=xxxx; yyyy=zzzz），
  粘贴到下面的「Cookie」框里即可。最前面如果有 "Cookie:" 这几个字，本工具会自动去掉。
  实测：不填 Cookie 也能下载上面那几项免费内容；填了更稳妥（比如站点改规则时）。

【运行】
  python 人人钢琴曲谱下载器.py
  依赖：仅 Python 标准库（tkinter / urllib / csv / threading），不需要 pip install。

说明：
  · 全程单线程串行 + 可调间隔，就是为了「慢慢来下」，不冲击站点、不触发限流。
  · 断点续跑：已存在且校验通过的文件不会重复下载，随时关掉下次接着跑。
  · 每个文件都做完整性校验（魔数 / 是否 1382 字节的伪 404 页），不合格会记录原因。
"""

import os
import re
import csv
import sys
import json
import time
import queue
import threading
import urllib.parse
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
BASE = "https://www.everyonepiano.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FAKE_404_SIZE = 1382                 # 站点对不存在的资源返回 200 + 1382 字节 HTML
ILLEGAL_CHARS = '<>:"/\\|?*'

# 文件名 -> 校验函数（返回 None 表示通过，否则返回错误说明）
def _check_png(b):
    return None if b[:8] == b"\x89PNG\r\n\x1a\n" else "不是有效的 PNG"

def _check_jpg(b):
    return None if b[:2] == b"\xff\xd8" else "不是有效的 JPEG"

def _check_eop(b):
    if b[:2] == b"4\x04":
        return None
    if b.startswith(b"<!DOCTYPE") or b.startswith(b"<html"):
        return "拿到的是 HTML（可能是 404 页）"
    return "EOP 文件头不匹配"

def _check_eopm(b):
    if b[:13] == b"EveryonePiano":
        return None
    if b.startswith(b"<!DOCTYPE") or b.startswith(b"<html"):
        return "拿到的是 HTML（可能是 404 页）"
    return "EOPM 文件头不匹配"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def encode_url(url):
    """中文/空格路径必须百分号编码，否则 urllib 会抛 UnicodeEncodeError。"""
    return urllib.parse.quote(url, safe=":/?&=%#+")


def safe_name(text, limit=80):
    """清掉 Windows 文件名里不能用的字符。"""
    text = "".join(ch for ch in text if ch >= " " and ch not in ILLEGAL_CHARS)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(". ")
    return text[:limit] or "未命名"


def parse_id_spec(text):
    """把 '1-50, 100, 20272' 之类的输入解析成升序编号列表。"""
    ids = set()
    for token in re.split(r"[,，;；\s]+", text.strip()):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            ids.update(range(a, b + 1))
        elif token.isdigit():
            ids.add(int(token))
    return sorted(ids)


class Fetcher:
    """带重试的 HTTP 客户端。"""

    def __init__(self, cookie="", timeout=30, retries=3, log=None, stop_event=None):
        self.cookie = cookie.strip()
        self.timeout = timeout
        self.retries = retries
        self.log = log or (lambda *a: None)
        self.stop_event = stop_event

    def get(self, path_or_url):
        """返回 (status, headers, bytes)；失败抛最后一次异常。"""
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        url = encode_url(url)
        last_err = None
        for attempt in range(1, self.retries + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                raise RuntimeError("已停止")
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Referer", BASE + "/Music.html")
            req.add_header("Accept", "*/*")
            if self.cookie:
                req.add_header("Cookie", self.cookie)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                # 4xx 直接放弃重试意义不大，但 5xx 值得重试
                last_err = e
                if e.code and 400 <= e.code < 500:
                    break
            except Exception as e:                       # 超时 / 连接重置 / DNS…
                last_err = e
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))         # 退避
        raise last_err

    def text(self, path_or_url):
        return self.get(path_or_url)[2].decode("utf-8", "ignore")


# --------------------------------------------------------------------------- #
# 解析单曲页
# --------------------------------------------------------------------------- #
RE_EOP = re.compile(r'href="(/Music/down/(\d+)/(\d+)/[^"]+)"')
RE_PM_EXACT = r"pianomusic/(\d+)/%s/"
RE_EOPM_PAGE = re.compile(r'href="(/Eopm-down-\d+\.html)"')
RE_EOPM_FILE = re.compile(r'href="(/Music/EopFile/[^"]+)"')
RE_W_COUNT = re.compile(r"五线谱预览\s*<small>\(\s*共\s*(\d+)\s*张")
RE_J_COUNT = re.compile(r"双手简谱预览\s*<small>\(\s*共\s*(\d+)\s*张")
RE_PM_DIR = re.compile(r"pianomusic/(\d+)/(\d+)/")
RE_H1 = re.compile(r"<h1>钢琴谱：\s*(.*?)\s*</h1>", re.S)
RE_TITLE = re.compile(r"<title>【谱】(.*?)-人人钢琴网</title>", re.S)


def parse_detail(html_text):
    """返回 dict 或 None（None = 该编号没有曲谱）。"""
    eop = RE_EOP.search(html_text)
    title = ""
    m = RE_H1.search(html_text)
    if m:
        title = m.group(1).strip()
    if not title:
        m = RE_TITLE.search(html_text)
        if m:
            title = m.group(1).strip()
    if not eop:
        return None                                   # 没有下载链接 = 编号不存在
    # href 形如 /Music/down/20272/0020272/曲名 —— 第 4 段才是 7 位补齐编号
    padded = eop.group(3)
    # 预览图目录必须按“补齐编号”精确匹配，否则会误取侧栏热门曲谱的目录
    pm = re.search(RE_PM_EXACT % padded, html_text) or RE_PM_DIR.search(html_text)
    dirnum = pm.group(1) if pm else "%03d" % ((int(padded) + 999) // 1000)
    m2 = RE_W_COUNT.search(html_text)
    m3 = RE_J_COUNT.search(html_text)
    m4 = RE_EOPM_PAGE.search(html_text)
    return {
        "title": title or padded,
        "padded": padded,
        "dir": dirnum,
        "eop": eop.group(1),
        "eopm_page": m4.group(1) if m4 else None,
        "w_count": int(m2.group(1)) if m2 else 0,
        "j_count": int(m3.group(1)) if m3 else 0,
    }


# --------------------------------------------------------------------------- #
# 任务主体
# --------------------------------------------------------------------------- #
class Job(threading.Thread):
    def __init__(self, opts, ui):
        super().__init__(daemon=True)
        self.o = opts
        self.ui = ui
        self.stop_event = threading.Event()

    # ---- 日志 ---------------------------------------------------------- #
    def log(self, msg, level="info"):
        self.ui.q.put(("log", level, msg))

    def stat(self, key, value=None):
        self.ui.q.put(("stat", key, value))

    # ---- 单文件下载 ------------------------------------------------------ #
    def grab(self, fetcher, path, dest, checker, label):
        """下载一个文件到 dest。返回 (状态, 说明)。
        状态：ok / skip / miss / fail"""
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            try:
                with open(dest, "rb") as f:
                    err = checker(f.read(64))
            except Exception:
                err = "读取失败"
            if err is None:
                return "skip", ""
        if self.o["dry_run"]:
            try:
                _, _, body = fetcher.get(path)
            except Exception as e:
                return "fail", "%s 探测失败：%s" % (label, e)
            if len(body) == FAKE_404_SIZE and body.startswith(b"<!DOCTYPE"):
                return "miss", ""
            if checker(body) is not None:
                return "miss", ""
            return "ok", "预检存在"

        try:
            _, _, body = fetcher.get(path)
        except Exception as e:
            return "fail", "%s 网络失败：%s" % (label, e)

        if len(body) == FAKE_404_SIZE and body.startswith(b"<!DOCTYPE"):
            return "miss", ""
        err = checker(body[:64])
        if err is not None:
            return "miss", "%s %s" % (label, err)

        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, dest)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return "fail", "%s 写盘失败：%s" % (label, e)
        time.sleep(self.o["gap_file"])
        return "ok", ""

    # ---- 单首曲谱 -------------------------------------------------------- #
    def one_song(self, fetcher, sid, outdir):
        padded = "%07d" % sid
        detail_url = "/Music-%d.html" % sid
        try:
            html_text = fetcher.text(detail_url)
        except Exception as e:
            return None, "详情页失败：%s" % e

        info = parse_detail(html_text)
        if info is None:
            return None, "编号不存在"

        padded = info["padded"]
        folder = os.path.join(outdir, "%s-%s" % (padded, safe_name(info["title"])))
        os.makedirs(folder, exist_ok=True)

        got, notes = [], []

        # EOP
        if "eop" in self.o["kinds"]:
            dest = os.path.join(folder, "%s.eop" % padded)
            st, why = self.grab(fetcher, info["eop"], dest, _check_eop, "EOP")
            self._tally("eop", st, notes, why)
            if st in ("ok", "skip"):
                got.append("EOP")

        # EOPM
        if "eopm" in self.o["kinds"] and info["eopm_page"]:
            link = None
            try:
                page = fetcher.text(info["eopm_page"])
                m = RE_EOPM_FILE.search(page)
                link = m.group(1) if m else None
            except Exception as e:
                notes.append("EOPM 页面失败：%s" % e)
            if link:
                dest = os.path.join(folder, "%s.eopm" % padded)
                st, why = self.grab(fetcher, link, dest, _check_eopm, "EOPM")
                self._tally("eopm", st, notes, why)
                if st in ("ok", "skip"):
                    got.append("EOPM")
            else:
                notes.append("无 EOPM")
                self._tally("eopm", "miss")

        # 五线谱大图
        if "wpng" in self.o["kinds"] and info["w_count"]:
            sts, ok_n = [], 0
            for n in range(1, info["w_count"] + 1):
                p = "/pianomusic/%s/%s/%s-w-b-%d.png" % (info["dir"], padded, padded, n)
                dest = os.path.join(folder, "%s-w-b-%d.png" % (padded, n))
                st, why = self.grab(fetcher, p, dest, _check_png, "五线谱第%d页" % n)
                sts.append(st)
                if st in ("ok", "skip"):
                    ok_n += 1
                elif st == "fail":
                    notes.append(why)
            self._tally("wpng", self._group_state(sts), notes,
                        "" if ok_n == info["w_count"] else
                        "五线谱 %d/%d 张" % (ok_n, info["w_count"]))
            if ok_n:
                got.append("五线谱×%d" % ok_n)

        # 双手简谱大图
        if "jpng" in self.o["kinds"] and info["j_count"]:
            sts, ok_n = [], 0
            for n in range(1, info["j_count"] + 1):
                p = "/pianomusic/%s/%s/%s-j-b-%d.png" % (info["dir"], padded, padded, n)
                dest = os.path.join(folder, "%s-j-b-%d.png" % (padded, n))
                st, why = self.grab(fetcher, p, dest, _check_png, "简谱第%d页" % n)
                sts.append(st)
                if st in ("ok", "skip"):
                    ok_n += 1
                elif st == "fail":
                    notes.append(why)
            self._tally("jpng", self._group_state(sts), notes,
                        "" if ok_n == info["j_count"] else
                        "简谱 %d/%d 张" % (ok_n, info["j_count"]))
            if ok_n:
                got.append("简谱×%d" % ok_n)

        # 缩略图（默认关）
        if "thumb" in self.o["kinds"]:
            for tag, cnt in (("w", info["w_count"]), ("j", info["j_count"])):
                for n in range(1, cnt + 1):
                    p = "/pianomusic/%s/%s/%s-%s-s-%d.jpg" % (info["dir"], padded, padded, tag, n)
                    dest = os.path.join(folder, "%s-%s-s-%d.jpg" % (padded, tag, n))
                    st, why = self.grab(fetcher, p, dest, _check_jpg, "缩略图")
                    if st == "fail":
                        notes.append(why)

        if not got:
            return ("无可用文件", folder), "; ".join(notes)
        return (",".join(got), folder), "; ".join(notes)

    def _tally(self, key, status, notes, why=""):
        self.ui.q.put(("tally", key, status))
        if why:
            notes.append(why)

    @staticmethod
    def _group_state(sts):
        """把一组页的状态归并成一个：全部命中缓存算 skip，共用一个下载算 ok。"""
        if not sts:
            return "miss"
        if all(s == "skip" for s in sts):
            return "skip"
        if all(s in ("ok", "skip") for s in sts):
            return "ok"
        return "miss"

    def report(self, sid, title, folder, state, note):
        self.ui.q.put(("row", sid, title, folder, state, note))

    # ---- 主循环 ---------------------------------------------------------- #
    def run(self):
        o = self.o
        outdir = o["outdir"]
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self.ui.q.put(("done", "目录创建失败：%s" % e, 0, 0, 0))
            return

        manifest_path = os.path.join(outdir, "曲谱清单.csv")
        log_path = os.path.join(outdir, "下载日志.txt")

        fetcher = Fetcher(cookie=o["cookie"], timeout=o["timeout"],
                          retries=o["retries"], log=self.log, stop_event=self.stop_event)

        ids = o["ids"]
        total = len(ids)
        self.stat("total", total)

        new_manifest = not os.path.exists(manifest_path)
        try:
            mf = open(manifest_path, "a", newline="", encoding="utf-8-sig")
            w = csv.writer(mf)
            if new_manifest:
                w.writerow(["编号", "曲名", "文件夹", "已获得", "状态", "备注", "时间"])
                mf.flush()
            lg = open(log_path, "a", encoding="utf-8")
        except Exception as e:
            self.ui.q.put(("done", "无法写入清单/日志：%s" % e, 0, 0, 0))
            return

        ok = bad = skip = 0
        consec_fail = 0
        t0 = time.time()

        for i, sid in enumerate(ids, 1):
            if self.stop_event.is_set():
                break
            try:
                result, note = self.one_song(fetcher, sid, outdir)
            except Exception as e:
                result, note = None, "未预期错误：%s" % e

            if result is None:
                state, folder = "跳过", ""
                title = ""
                skip += 1
                lg.write("[%s] #%d 不存在：%s\n" % (time.strftime("%H:%M:%S"), sid, note))
            else:
                got, folder = result
                state = "完成" if got != "无可用文件" else "无文件"
                title = os.path.basename(folder).split("-", 1)[-1] if folder else ""
                if state == "完成":
                    ok += 1
                else:
                    bad += 1
                if note:
                    lg.write("[%s] #%d %s -> %s | %s\n"
                             % (time.strftime("%H:%M:%S"), sid, title, got, note))

            w.writerow([sid, title, folder, "" if result is None else result[0],
                        state, note, time.strftime("%Y-%m-%d %H:%M:%S")])
            mf.flush()
            lg.flush()

            self.report(sid, title, folder, state, note)
            self.ui.q.put(("progress", i, total))
            self.ui.q.put(("stats", ok, bad, skip, time.time() - t0))

            # 连续网络失败保护
            if note and ("网络失败" in note or "详情页失败" in note):
                consec_fail += 1
                if consec_fail >= 8:
                    self.ui.q.put(("done",
                                   "连续 8 次网络失败，可能被站点限流或断网，已自动停止。"
                                   "建议加大间隔、稍后再从当前编号继续。", ok, bad, skip))
                    mf.close(); lg.close()
                    return
            else:
                consec_fail = 0

            if i < total:
                time.sleep(o["gap_song"])

        mf.close(); lg.close()
        self.ui.q.put(("done", "", ok, bad, skip))


# --------------------------------------------------------------------------- #
# 面板
# --------------------------------------------------------------------------- #
class App:
    @staticmethod
    def _base_dir():
        """设置文件的存放位置：打包成 exe 后 __file__ 指向临时解包目录（退出即删），
        必须改用 exe 自身所在目录，否则设置与记住的 Cookie 每次都丢。"""
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.abspath(__file__))

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.job = None
        self.rows = {}
        self.settings_path = os.path.join(self._base_dir(), "下载器设置.json")
        self._build()
        self._load_settings()
        self.root.after(120, self._pump)

    # ---- 界面 ------------------------------------------------------------ #
    def _build(self):
        self.root.title("人人钢琴曲谱下载器  ·  everyonepiano.cn")
        self.root.geometry("960x820")
        self.root.minsize(900, 700)

        pad = dict(padx=8, pady=4)
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=10, pady=8)

        # —— 账号 ---------------------------------------------------------- #
        f1 = ttk.LabelFrame(main, text=" 账号（可选） ")
        f1.pack(fill="x", **pad)
        ttk.Label(f1, text="Cookie：").grid(row=0, column=0, sticky="nw", padx=6, pady=6)
        self.cookie = tk.Text(f1, height=3, wrap="char",
                              font=("Consolas", 9))
        self.cookie.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=6)
        f1.columnconfigure(1, weight=1)
        ttk.Label(f1,
                  text="留空也能下载免费内容；要填就粘贴浏览器 F12 → Network 里的整串 Cookie。"
                       "开头的 “Cookie:” 会自动去掉。",
                  foreground="#666").grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        self.remember_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(f1, text="在这台电脑上记住 Cookie（明文保存在本工具同目录，"
                                 "不建议在公用电脑勾选）",
                        variable=self.remember_cookie).grid(row=2, column=1, sticky="w", padx=6, pady=(0, 6))

        # —— 保存目录 ------------------------------------------------------ #
        f2 = ttk.LabelFrame(main, text=" 保存目录 ")
        f2.pack(fill="x", **pad)
        self.outdir = tk.StringVar(value=r"D:\人人钢琴曲谱数据")
        ttk.Entry(f2, textvariable=self.outdir).grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        ttk.Button(f2, text="浏览…", command=self._pick_dir).grid(row=0, column=1, padx=6)
        f2.columnconfigure(0, weight=1)

        # —— 下载内容 ------------------------------------------------------ #
        f3 = ttk.LabelFrame(main, text=" 下载内容（全部免费，VIP 的 MP3 / PDF / EOPN / MIDI 一律不下载） ")
        f3.pack(fill="x", **pad)
        self.k_eop = tk.BooleanVar(value=True)
        self.k_eopm = tk.BooleanVar(value=True)
        self.k_wpng = tk.BooleanVar(value=True)
        self.k_jpng = tk.BooleanVar(value=True)
        self.k_thumb = tk.BooleanVar(value=False)
        ttk.Checkbutton(f3, text="EOP 文件 (.eop)", variable=self.k_eop).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(f3, text="EOPM 文件 (.eopm)", variable=self.k_eopm).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(f3, text="五线谱大图 PNG", variable=self.k_wpng).grid(row=0, column=2, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(f3, text="双手简谱大图 PNG", variable=self.k_jpng).grid(row=0, column=3, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(f3, text="缩略图 JPG（200×260，不太清晰，一般不用勾）",
                        variable=self.k_thumb).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))

        # —— 范围 ---------------------------------------------------------- #
        f4 = ttk.LabelFrame(main, text=" 下载范围 ")
        f4.pack(fill="x", **pad)
        self.mode = tk.StringVar(value="range")
        ttk.Radiobutton(f4, text="按编号区间", variable=self.mode,
                        value="range").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.id_from = tk.StringVar(value="1")
        self.id_to = tk.StringVar(value="20300")
        ttk.Entry(f4, textvariable=self.id_from, width=9).grid(row=0, column=1, padx=2)
        ttk.Label(f4, text="到").grid(row=0, column=2)
        ttk.Entry(f4, textvariable=self.id_to, width=9).grid(row=0, column=3, padx=2)
        ttk.Label(f4, text="（站点曲谱编号，全站约 1~20284，不存在的会自动跳过）",
                  foreground="#666").grid(row=0, column=4, sticky="w", padx=8)
        ttk.Radiobutton(f4, text="指定编号", variable=self.mode,
                        value="list").grid(row=1, column=0, sticky="nw", padx=6)
        self.id_list = tk.Text(f4, height=2, wrap="char", font=("Consolas", 9))
        self.id_list.grid(row=1, column=1, columnspan=4, sticky="ew", padx=2, pady=(0, 6))
        ttk.Label(f4, text="支持 “1-50, 100, 20272” 这种写法，逗号/空格/换行分隔",
                  foreground="#666").grid(row=2, column=1, columnspan=4, sticky="w", padx=2)
        f4.columnconfigure(4, weight=1)

        # —— 节奏 ---------------------------------------------------------- #
        f5 = ttk.LabelFrame(main, text=" 节奏与重试（想慢就调大间隔） ")
        f5.pack(fill="x", **pad)
        self.gap_song = tk.StringVar(value="1.5")
        self.gap_file = tk.StringVar(value="0.3")
        self.retries = tk.StringVar(value="3")
        self.timeout = tk.StringVar(value="30")
        labels = [("每首曲谱之间等待(秒)", self.gap_song, 5),
                  ("每个文件之间等待(秒)", self.gap_file, 5),
                  ("失败重试次数", self.retries, 5),
                  ("网络超时(秒)", self.timeout, 5)]
        for i, (txt, var, w) in enumerate(labels):
            ttk.Label(f5, text=txt).grid(row=0, column=i * 2, sticky="e", padx=(8, 2), pady=6)
            ttk.Entry(f5, textvariable=var, width=w).grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 6))
        self.dry_run = tk.BooleanVar(value=False)
        ttk.Checkbutton(f5, text="仅预检：只统计有哪些文件、不真正下载（先看规模用）",
                        variable=self.dry_run).grid(row=1, column=0, columnspan=8, sticky="w", padx=8, pady=(0, 6))

        # —— 按钮 ---------------------------------------------------------- #
        f6 = ttk.Frame(main)
        f6.pack(fill="x", **pad)
        self.btn_start = ttk.Button(f6, text="开始下载", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(f6, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(f6, text="打开保存目录", command=self.open_dir).pack(side="left", padx=8)
        self.resume_note = tk.Label(f6, text="已支持断点续跑：重复运行不会重复下载",
                                    fg="#666")
        self.resume_note.pack(side="right")

        # —— 进度 ---------------------------------------------------------- #
        f7 = ttk.Frame(main)
        f7.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(f7, mode="determinate")
        self.bar.pack(fill="x", side="top")
        self.stat_lbl = tk.Label(f7, text="等待开始", anchor="w", font=("Microsoft YaHei", 9))
        self.stat_lbl.pack(fill="x", pady=(2, 0))

        # —— 日志 ---------------------------------------------------------- #
        f8 = ttk.LabelFrame(main, text=" 运行日志 ")
        f8.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(f8, height=14, wrap="word",
                                font=("Microsoft YaHei", 9), state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.tag_config("err", foreground="#c0392b")
        self.log.tag_config("ok", foreground="#1e8449")
        self.log.tag_config("warn", foreground="#b9770e")

    # ---- 设置读写 -------------------------------------------------------- #
    def _settings(self):
        d = {
            "outdir": self.outdir.get(),
            "kinds": {"eop": self.k_eop.get(), "eopm": self.k_eopm.get(),
                      "wpng": self.k_wpng.get(), "jpng": self.k_jpng.get(),
                      "thumb": self.k_thumb.get()},
            "mode": self.mode.get(),
            "id_from": self.id_from.get(), "id_to": self.id_to.get(),
            "id_list": self.id_list.get("1.0", "end").strip(),
            "gap_song": self.gap_song.get(), "gap_file": self.gap_file.get(),
            "retries": self.retries.get(), "timeout": self.timeout.get(),
            "dry_run": self.dry_run.get(),
            "remember_cookie": self.remember_cookie.get(),
            "cookie": self.cookie.get("1.0", "end").strip() if self.remember_cookie.get() else "",
        }
        return d

    def _save_settings(self):
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(self._settings(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_settings(self):
        if not os.path.exists(self.settings_path):
            return
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        self.outdir.set(d.get("outdir", self.outdir.get()))
        k = d.get("kinds", {})
        self.k_eop.set(k.get("eop", True))
        self.k_eopm.set(k.get("eopm", True))
        self.k_wpng.set(k.get("wpng", True))
        self.k_jpng.set(k.get("jpng", True))
        self.k_thumb.set(k.get("thumb", False))
        self.mode.set(d.get("mode", "range"))
        self.id_from.set(d.get("id_from", "1"))
        self.id_to.set(d.get("id_to", "20300"))
        if d.get("id_list"):
            self.id_list.insert("1.0", d["id_list"])
        self.gap_song.set(d.get("gap_song", "1.5"))
        self.gap_file.set(d.get("gap_file", "0.3"))
        self.retries.set(d.get("retries", "3"))
        self.timeout.set(d.get("timeout", "30"))
        self.dry_run.set(d.get("dry_run", False))
        if d.get("remember_cookie") and d.get("cookie"):
            self.remember_cookie.set(True)
            self.cookie.insert("1.0", d["cookie"])

    # ---- 动作 ------------------------------------------------------------ #
    def _pick_dir(self):
        p = filedialog.askdirectory(initialdir=self.outdir.get() or "D:\\")
        if p:
            self.outdir.set(os.path.normpath(p))

    def open_dir(self):
        p = self.outdir.get()
        if not os.path.isdir(p):
            messagebox.showinfo("提示", "目录还不存在：\n%s" % p)
            return
        try:
            os.startfile(p)
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    def _num(self, var, default, cast=float):
        try:
            return cast(var.get())
        except Exception:
            return default

    def start(self):
        outdir = self.outdir.get().strip()
        if not outdir:
            messagebox.showwarning("缺少目录", "请先选择保存目录。")
            return
        kinds = [k for k, v in (("eop", self.k_eop.get()), ("eopm", self.k_eopm.get()),
                                ("wpng", self.k_wpng.get()), ("jpng", self.k_jpng.get()),
                                ("thumb", self.k_thumb.get())) if v]
        if not kinds:
            messagebox.showwarning("没选内容", "至少勾选一种要下载的内容。")
            return
        if self.mode.get() == "range":
            try:
                a, b = int(self.id_from.get()), int(self.id_to.get())
            except Exception:
                messagebox.showwarning("编号不对", "编号区间要填整数。")
                return
            if a > b:
                a, b = b, a
            if b - a > 30000:
                if not messagebox.askyesno("范围很大",
                                           "编号区间有 %d 个，确定继续？" % (b - a + 1)):
                    return
            ids = list(range(a, b + 1))
        else:
            ids = parse_id_spec(self.id_list.get("1.0", "end"))
            if not ids:
                messagebox.showwarning("编号为空", "请填要下载的编号，例如 1-50, 20272。")
                return

        cookie = self.cookie.get("1.0", "end").strip()
        cookie = re.sub(r"^\s*cookie\s*:\s*", "", cookie, flags=re.I)
        cookie = " ".join(cookie.split())

        opts = {
            "outdir": outdir, "kinds": set(kinds), "ids": ids, "cookie": cookie,
            "gap_song": max(0.0, self._num(self.gap_song, 1.5)),
            "gap_file": max(0.0, self._num(self.gap_file, 0.3)),
            "retries": max(1, self._num(self.retries, 3, int)),
            "timeout": max(5, self._num(self.timeout, 30, int)),
            "dry_run": self.dry_run.get(),
        }
        self._save_settings()

        self.rows.clear()
        self.log.config(state="normal"); self.log.delete("1.0", "end"); self.log.config(state="disabled")
        self.bar["value"] = 0
        self.bar["maximum"] = len(ids)
        self._log("开始：共 %d 个编号，内容 = %s%s" %
                  (len(ids), "、".join(kinds), "（仅预检，不下载）" if opts["dry_run"] else ""))
        if not cookie:
            self._log("未填 Cookie：按实测，免费内容不登录也能下；若遇到异常再补填。", "warn")

        self.job = Job(opts, self)
        self.job.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

    def stop(self):
        if self.job and self.job.is_alive():
            self.job.stop_event.set()
            self._log("已请求停止，正在收尾（当前文件下完就停）…", "warn")
        self.btn_stop.config(state="disabled")

    # ---- 日志 / 队列 ------------------------------------------------------ #
    def _log(self, msg, level="info"):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n", level)
        self.log.see("end")
        self.log.config(state="disabled")

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[2], item[1])
                elif kind == "progress":
                    self.bar["value"] = item[1]
                elif kind == "stats":
                    ok, bad, skip, secs = item[1], item[2], item[3], item[4]
                    rate = (ok + bad + skip) / secs if secs > 0 else 0
                    self.stat_lbl.config(
                        text="完成 %d 首 · 无文件 %d 首 · 跳过 %d 个编号 · 用时 %.0f 秒 · 平均 %.2f 首/秒"
                             % (ok, bad, skip, secs, rate))
                elif kind == "row":
                    sid, title, folder, state, note = item[1:]
                    self._log("#%d %s  [%s]%s" % (sid, title or "-", state,
                                                  ("  " + note) if note else ""),
                              "err" if state in ("无文件", "跳过") else "ok")
                elif kind == "done":
                    msg, ok, bad, skip = item[1], item[2], item[3], item[4]
                    if msg:
                        self._log(msg, "err")
                    self._log("结束：完成 %d 首，无可用文件 %d 首，跳过不存在 %d 个编号。"
                              "清单见保存目录下的 曲谱清单.csv" % (ok, bad, skip), "ok")
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._pump)


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.15)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
