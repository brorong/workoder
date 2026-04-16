"""
配件安裝系統 — API 功能測試腳本
直接在同一 process 內啟動 Flask test client，不需外部伺服器
"""
import sys
import json
import io
import unittest

# 確保可以 import app
sys.path.insert(0, ".")
from app import app, init_db

ADMIN_TOKEN = "dev-token-123"  # 從 .env 讀取
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


class APITestCase(unittest.TestCase):
    _order_id = None  # 跨測試共享工單 ID

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        pass

    # ── 健康檢查 ───────────────────────────────────────────
    def test_01_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        print("  OK 健康檢查 OK")

    # ── 認證 ───────────────────────────────────────────────
    def test_02_auth_required(self):
        r = self.client.get("/api/users")
        self.assertEqual(r.status_code, 401)
        print("  OK 無 Token 回 401 OK")

    def test_03_auth_token_valid(self):
        r = self.client.get("/api/users", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        print("  OK 正確 Token 通過驗證 OK")

    # ── 使用者 ─────────────────────────────────────────────
    def test_04_list_users(self):
        r = self.client.get("/api/users", headers=HEADERS)
        users = r.get_json()
        self.assertIsInstance(users, list)
        roles = {u["role"] for u in users}
        self.assertIn("factory", roles)
        self.assertIn("installer", roles)
        print(f"  OK 使用者列表回傳 {len(users)} 筆 OK")

    def test_05_create_user(self):
        payload = {"line_id": "U_test999", "name": "測試技師", "role": "installer"}
        r = self.client.post("/api/users", headers=HEADERS,
                             json=payload)
        self.assertIn(r.status_code, [201, 409])  # 201 新建 / 409 已存在
        print(f"  OK 建立使用者 HTTP {r.status_code} OK")

    def test_06_create_user_missing_field(self):
        r = self.client.post("/api/users", headers=HEADERS,
                             json={"line_id": "U_bad"})  # 缺少 name / role
        self.assertEqual(r.status_code, 400)
        print("  OK 缺少欄位回 400 OK")

    def test_07_create_user_dup(self):
        payload = {"line_id": "U_factory1", "name": "廠務王大明", "role": "factory"}
        r = self.client.post("/api/users", headers=HEADERS, json=payload)
        self.assertEqual(r.status_code, 409)
        print("  OK 重複 LINE ID 回 409 OK")

    # ── 工單建立 ────────────────────────────────────────────
    def test_10_create_order(self):
        payload = {
            "car_no": "ABC-1234",
            "car_type": "轎車",
            "engine_no": "ENG001",
            "location": "台北市",
            "install_date": "2026-04-20",
            "items": ["行車記錄器", "胎壓偵測"],
            "note": "測試工單"
        }
        r = self.client.post("/api/orders", headers=HEADERS, json=payload)
        self.assertEqual(r.status_code, 201)
        data = r.get_json()
        self.assertIn("order_id", data)
        APITestCase._order_id = data["order_id"]
        print(f"  OK 建立工單 {data['order_id']} OK")

    def test_11_create_order_missing_car_no(self):
        r = self.client.post("/api/orders", headers=HEADERS,
                             json={"car_type": "SUV"})
        self.assertEqual(r.status_code, 400)
        print("  OK 缺少 car_no 回 400 OK")

    # ── 工單查詢 ────────────────────────────────────────────
    def test_12_list_orders(self):
        r = self.client.get("/api/orders", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        orders = r.get_json()
        self.assertIsInstance(orders, list)
        print(f"  OK 工單列表回傳 {len(orders)} 筆 OK")

    def test_13_list_orders_filter_status(self):
        r = self.client.get("/api/orders?status=待派工", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        orders = r.get_json()
        for o in orders:
            self.assertEqual(o["status"], "待派工")
        print(f"  OK 狀態篩選「待派工」回 {len(orders)} 筆 OK")

    def test_14_get_order(self):
        oid = APITestCase._order_id
        r = self.client.get(f"/api/orders/{oid}")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["order_id"], oid)
        print(f"  OK 取得工單 {oid} OK")

    def test_15_get_order_not_found(self):
        r = self.client.get("/api/orders/ORD_NOT_EXIST")
        self.assertEqual(r.status_code, 404)
        print("  OK 不存在工單回 404 OK")

    # ── 工單流程 ────────────────────────────────────────────
    def test_20_assign_order(self):
        oid = APITestCase._order_id
        r = self.client.post(f"/api/orders/{oid}/assign", headers=HEADERS,
                             json={"installer_name": "技師陳大明"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "待確認")
        print(f"  OK 指派技師 → 待確認 OK")

    def test_21_assign_unknown_installer(self):
        oid = APITestCase._order_id
        r = self.client.post(f"/api/orders/{oid}/assign", headers=HEADERS,
                             json={"installer_name": "不存在技師"})
        self.assertEqual(r.status_code, 404)
        print("  OK 找不到技師回 404 OK")

    def test_22_arrive(self):
        oid = APITestCase._order_id
        r = self.client.post(f"/api/orders/{oid}/arrive")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        print(f"  OK 到場確認 → 施工中 OK")

    def test_23_submit(self):
        oid = APITestCase._order_id
        r = self.client.post(f"/api/orders/{oid}/submit")
        self.assertEqual(r.status_code, 200)
        print(f"  OK 提交完工 → 待審核 OK")

    def test_24_submit_wrong_status(self):
        oid = APITestCase._order_id
        # 已變成待審核，再次提交應 400
        r = self.client.post(f"/api/orders/{oid}/submit")
        self.assertEqual(r.status_code, 400)
        print(f"  OK 狀態不符再提交回 400 OK")

    def test_25_approve(self):
        oid = APITestCase._order_id
        r = self.client.post(f"/api/orders/{oid}/approve", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        print(f"  OK 審核通過 → 已完成 OK")

    def test_26_recall(self):
        # 新建一張工單測試收回
        payload = {"car_no": "ZZZ-9999", "items": ["收回測試"]}
        r = self.client.post("/api/orders", headers=HEADERS, json=payload)
        oid2 = r.get_json()["order_id"]
        self.client.post(f"/api/orders/{oid2}/assign", headers=HEADERS,
                         json={"installer_name": "技師林小華"})
        r = self.client.post(f"/api/orders/{oid2}/recall", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        # 確認狀態回到待派工
        r2 = self.client.get(f"/api/orders/{oid2}")
        self.assertEqual(r2.get_json()["status"], "待派工")
        print(f"  OK 收回工單 → 待派工 OK")

    def test_27_reject(self):
        # 新建一張工單走到待審核再退回
        payload = {"car_no": "REJ-0001", "items": ["退回測試"]}
        r = self.client.post("/api/orders", headers=HEADERS, json=payload)
        oid3 = r.get_json()["order_id"]
        self.client.post(f"/api/orders/{oid3}/assign", headers=HEADERS,
                         json={"installer_name": "技師陳大明"})
        self.client.post(f"/api/orders/{oid3}/arrive")
        self.client.post(f"/api/orders/{oid3}/submit")
        r = self.client.post(f"/api/orders/{oid3}/reject", headers=HEADERS,
                             json={"reason": "照片不清楚"})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get(f"/api/orders/{oid3}")
        self.assertEqual(r2.get_json()["status"], "退回")
        self.assertEqual(r2.get_json()["reject_reason"], "照片不清楚")
        print(f"  OK 退回工單 → 退回 + 原因 OK")

    # ── 照片上傳 ────────────────────────────────────────────
    def test_30_upload_photo(self):
        oid = APITestCase._order_id
        # 建立一張最小 JPEG (1x1 pixel)
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, format="JPEG")
        buf.seek(0)
        data = {"photo_type": "before"}
        r = self.client.post(
            f"/api/orders/{oid}/photos",
            data={"photo_type": "before", "file": (buf, "test.jpg", "image/jpeg")},
            content_type="multipart/form-data"
        )
        self.assertEqual(r.status_code, 201)
        print(f"  OK 照片上傳 OK")

    def test_31_upload_invalid_mime(self):
        oid = APITestCase._order_id
        buf = io.BytesIO(b"this is not an image")
        r = self.client.post(
            f"/api/orders/{oid}/photos",
            data={"photo_type": "other", "file": (buf, "bad.txt", "text/plain")},
            content_type="multipart/form-data"
        )
        self.assertEqual(r.status_code, 415)
        print(f"  OK 非圖片 MIME 回 415 OK")

    def test_32_list_photos(self):
        oid = APITestCase._order_id
        r = self.client.get(f"/api/orders/{oid}/photos")
        self.assertEqual(r.status_code, 200)
        photos = r.get_json()
        print(f"  OK 照片列表回傳 {len(photos)} 筆 OK")


if __name__ == "__main__":
    print("=" * 55)
    print("  配件安裝系統 API 功能測試")
    print("=" * 55)
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = lambda a, b: (a > b) - (a < b)
    suite = loader.loadTestsFromTestCase(APITestCase)
    runner = unittest.TextTestRunner(verbosity=0, stream=sys.stdout)
    result = runner.run(suite)
    print("=" * 55)
    print(f"  執行：{result.testsRun} 項  "
          f"通過：{result.testsRun - len(result.failures) - len(result.errors)}  "
          f"失敗：{len(result.failures)}  "
          f"錯誤：{len(result.errors)}")
    print("=" * 55)
    sys.exit(0 if result.wasSuccessful() else 1)
