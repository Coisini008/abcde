import cv2
import mediapipe as mp
import math
import time
import requests
import numpy as np
import json  # 1. 確保最上方有 import json
import os

# ==========================================
# 👥 個案標記與個別化 Config 讀取
# ==========================================
# 💡 【手動切換個案】要在這裡改！
# 想切換成阿公就改成 "B_Grandpa"，想切換成阿嬤就用 "A_Grandma"
CURRENT_PATIENT_ID = "A_Grandma"


def load_patient_config(patient_id):
    """從 JSON 檔案中讀取特定個案的專屬閾值"""
    # 🎯 這裡修正為絕對路徑，確保一定讀得到專案根目錄下的 JSON 檔
    json_path =json_path = r"C:\Users\MANDY\Desktop\patients.json"

    default_config = {
        "name": "未登錄個案",
        "MAR_OPEN_THRESHOLD": 0.15,
        "MAR_CLOSE_THRESHOLD": 0.06,
        "MAX_CHEW_TIME": 4.0,
        "CHEW_WAVE_THRESHOLD": 2.0,
        "SWALLOW_STILL_THRESHOLD": 0.8
    }

    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if patient_id in data:
                print(f"✅ 成功載入【{data[patient_id]['name']}】的個別化參數設定！")
                return data[patient_id]

    print("⚠️ 找不到設定檔或個案，使用系統預設參數。")
    return default_config


# 自動載入當前個案的專屬 Threshold！
CONFIG = load_patient_config(CURRENT_PATIENT_ID)

# 補上固定參數
CONFIG["SWALLOW_DURATION"] = 0.6
CONFIG["SAFETY_CHECK_DURATION"] = 4.0

# ==========================================
# 🗄️ 3. SQLite 資料庫配置
# ==========================================
import sqlite3

DB_PATH = r"C:\Users\MANDY\PyCharmMiscProject\eating_records.db"

def init_database():
    """初始化資料庫，建立進食紀錄表"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 建立表格（欄位：流水號、個案ID、姓名、時間、咀嚼次數、狀態/結果）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eating_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT,
            patient_name TEXT,
            timestamp TEXT,
            chew_count INTEGER,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_eating_record(patient_id, patient_name, chew_count, status):
    """將進食結果寫入資料庫"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        cursor.execute('''
            INSERT INTO eating_records (patient_id, patient_name, timestamp, chew_count, status)
            VALUES (?, ?, ?, ?, ?)
        ''', (patient_id, patient_name, current_time_str, int(chew_count), status))
        conn.commit()
        conn.close()
        print(f"💾 【資料庫同步】已成功寫入 {patient_name} 的進食紀錄 ({status})！")
    except Exception as e:
        print(f"❌ 資料庫寫入失敗: {e}")

# 執行初始化（程式啟動時自動檢查並建立檔案）
init_database()
# ==========================================
# 🔒 1. LINE 通知 Token 配置
# ==========================================
LINE_TOKEN ="8PhT1cfYpL5L5YTVKBR80EIPoyYFbNGvcqvdQ52D7k0Y6hxsbYeQ2Ex3Ov+WBa2HwGFBrpZo/NucJszkxsokrMRTI9y7qL1fR92sbbhfg/rFHckViPMSBFwBbWBFI73bMb7gB4FAF1Kt8iBtCR7MpgdB04t89/1O/w1cDnyilFU="


def send_line_notification(message):
    """透過 LINE Messaging API 發送即時警報"""
    if not LINE_TOKEN: return
    # 注意：這裡網址和格式與之前的 LINE Notify 不同
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }
    data = {
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }
    try:
        res = requests.post(url, json=data, headers=headers)
        if res.status_code != 200:
            print(f"❌ LINE 發送失敗，狀態碼: {res.status_code}, 回應: {res.text}")
    except Exception as e:
        print(f"❌ LINE 連線異常: {e}")


# ==========================================
# 🎛️ 2. 核心參數調校（針對即時鏡頭與嗆咳優化）
# ==========================================
# 💡 註：重複覆蓋 CONFIG 的程式碼已按照之前的討論徹底移除，保留讀取 JSON 的動態覆蓋

# 狀態機狀態
ST_IDLE = "IDLE"  # 等待進食
ST_INGEST = "INGEST"  # 食物入口
ST_CHEW = "CHEW"  # 閉嘴咀嚼
ST_SWALLOW = "SWALLOW"  # 準備吞嚥
ST_CHECK = "CHECK"  # 安全期

current_state = ST_IDLE
state_start_time = time.time()

# 數據緩存
prev_jaw_y = None
jaw_movement_history = []
chew_count = 0

# ==========================================
# 🤖 3. 初始化 MediaPipe Face Mesh
# ==========================================
try:
    from mediapipe.python.solutions import face_mesh as mp_face_mesh
except ImportError:
    import mediapipe.solutions.face_mesh as mp_face_mesh

face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,  # 啟用內唇點位
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

# ==========================================
# 📹 4. 改為讀取電腦內建鏡頭 (Live)
# ==========================================
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("❌ 錯誤：無法開啟電腦內建鏡頭！請確認沒有其他程式（如 Zoom/Teams）正在佔用鏡頭。")
    exit()

print("🚀 內建鏡頭啟動成功！請將臉對準鏡頭開始測試...")

while cap.isOpened():
    success, frame = cap.read()
    if not success:
        print("❌ 錯誤：無法讀取鏡頭畫面。")
        break

    frame = cv2.flip(frame, 1)

    h, w, _ = frame.shape
    current_time = time.time()

    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = face_mesh.process(rgb_frame)

    if results.multi_face_landmarks:
        face_landmarks = results.multi_face_landmarks[0]

        p13 = face_landmarks.landmark[13]
        p14 = face_landmarks.landmark[14]
        p78 = face_landmarks.landmark[78]
        p308 = face_landmarks.landmark[308]
        p152 = face_landmarks.landmark[152]
        p4 = face_landmarks.landmark[4]

        v_dist = math.sqrt((p13.x - p14.x) ** 2 + (p13.y - p14.y) ** 2) * h
        h_dist = math.sqrt((p78.x - p308.x) ** 2 + (p78.y - p308.y) ** 2) * w
        mar = v_dist / h_dist if h_dist > 0 else 0

        jaw_relative_y = (p152.y - p4.y) * h
        jaw_movement_history.append(jaw_relative_y)
        if len(jaw_movement_history) > 30:
            jaw_movement_history.pop(0)

        jaw_velocity = 0
        if prev_jaw_y is not None:
            jaw_velocity = jaw_relative_y - prev_jaw_y
        prev_jaw_y = jaw_relative_y

        # ==========================================
        # 🧠 5. 時序狀態機控制邏輯
        # ==========================================
        if len(jaw_movement_history) >= 15:
            recent_movement = jaw_movement_history[-15:]
            movement_std = np.std(recent_movement)
        else:
            movement_std = 0

        if current_state == ST_IDLE:
            if mar > CONFIG["MAR_OPEN_THRESHOLD"]:
                current_state = ST_INGEST
                state_start_time = current_time
                chew_count = 0
                print("【通知】偵測到張嘴：食物入口 🍛")

        elif current_state == ST_INGEST:
            if mar < CONFIG["MAR_CLOSE_THRESHOLD"]:
                current_state = ST_CHEW
                state_start_time = current_time
                print("【通知】開始閉嘴咀嚼食物 🦷")

        elif current_state == ST_CHEW:
            if movement_std > CONFIG["CHEW_WAVE_THRESHOLD"] and abs(jaw_velocity) > 1.5:
                if len(jaw_movement_history) >= 3:
                    if (jaw_movement_history[-1] - jaw_movement_history[-2]) * (
                            jaw_movement_history[-2] - jaw_movement_history[-3]) < 0:
                        chew_count += 0.5

            if movement_std > 15.0 and abs(jaw_velocity) > 8.0:
                cough_msg = f"🚨【緊急警報】偵測到出現劇烈嗆咳、身體猛烈晃動！請立即前往協助！"
                print(cough_msg)
                send_line_notification(cough_msg)

                # 🎯 在這裡補上這行：把嗆咳紀錄存進資料庫！
                save_eating_record(CURRENT_PATIENT_ID, CONFIG.get('name', '未知個案'), chew_count, "劇烈嗆咳警報")

                state_start_time = current_time


            # 只有在「超過時間」而且「下巴真的完全靜止不動」時，才發送卡喉警告！

            elif (current_time - state_start_time) > CONFIG["MAX_CHEW_TIME"] and movement_std < CONFIG[
                "SWALLOW_STILL_THRESHOLD"]:
                warn_msg = f"⚠️【哽噎預警】已停止咀嚼或卡喉超過 {CONFIG['MAX_CHEW_TIME']} 秒，請注意！"
                print(warn_msg)
                send_line_notification(warn_msg)
                state_start_time = current_time

            if movement_std < CONFIG["SWALLOW_STILL_THRESHOLD"] and len(jaw_movement_history) >= 15:
                current_state = ST_SWALLOW
                state_start_time = current_time
                print("【通知】咀嚼停止，下巴上提（定格吞嚥中...）")


        elif current_state == ST_SWALLOW:

            if (current_time - state_start_time) > CONFIG["SWALLOW_DURATION"]:

                current_state = ST_CHECK

                state_start_time = current_time

                print(f"🎉【數據分析】吞嚥成功！本次進食週期共咀嚼約 {int(chew_count)} 次。進入安全期。")

                # 🎯 在這裡補上這行：把成功吞嚥紀錄存進資料庫！

                save_eating_record(CURRENT_PATIENT_ID, CONFIG.get('name', '未知個案'), chew_count, "吞嚥成功")
            elif movement_std > CONFIG["CHEW_WAVE_THRESHOLD"]:
                current_state = ST_CHEW
                print("【狀態回退】非吞嚥，恢復咀嚼。")

        elif current_state == ST_CHECK:
            if (current_time - state_start_time) > CONFIG["SAFETY_CHECK_DURATION"]:
                print("💖【數據分析】安全通過進食觀察期。")
                current_state = ST_IDLE


                # ==========================================
                # 📺 6. 視覺化介面繪製 (UI) - 修正放左上角整齊排版
                # ==========================================
                for idx in [13, 14, 78, 308, 4, 152]:
                    pt = face_landmarks.landmark[idx]
                    cv2.circle(frame, (int(pt.x * w), int(pt.y * h)), 4, (0, 255, 0), -1)

                cv2.line(frame, (int(p13.x * w), int(p13.y * h)), (int(p14.x * w), int(p14.y * h)), (255, 0, 0), 1)
                cv2.line(frame, (int(p4.x * w), int(p4.y * h)), (int(p152.x * w), int(p152.y * h)), (0, 0, 255), 2)

                # 確保名字正確
                CONFIG['name'] = "李亦容"
                cv2.putText(frame, f"Patient: {CONFIG.get('name', '未知個案')}", (20, 35), cv2.FONT_HERSHEY_SIMPLEX,0.6, (255, 200, 100), 2)
                cv2.putText(frame, f"STATE: {current_state}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                cv2.putText(frame, f"MAR (Mouth Open): {mar:.2f}", (20, 105), cv2.FONT_HERSHEY_SIMPLEX, 0.6,(255, 255, 255), 1)
                cv2.putText(frame, f"Jaw Movement Std: {movement_std:.2f}", (20, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.6,(255, 255, 255), 1)
                cv2.putText(frame, f"Chew Count: {int(chew_count)}", (20, 175), cv2.FONT_HERSHEY_SIMPLEX, 0.6,(100, 255, 100), 2)

                # 🎯 修正縮排：把 SWALLOWING 放到有偵測到臉的區域
                if current_state == ST_SWALLOW:
                    cv2.putText(frame, "SWALLOWING...", (20, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)

            else:
                # 🎯 修正縮排：只有在「沒偵測到人臉 (else)」時，才在畫面中央顯示紅色警告！
                cv2.putText(frame, "NO FACE DETECTED", (140, int(h / 2)), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)

            cv2.imshow('Anti-Choking Live Analysis System', frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("🛑 使用者關閉即時系統。")
                break

cap.release()
cv2.destroyAllWindows()