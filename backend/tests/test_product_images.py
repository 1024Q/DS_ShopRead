import tempfile
import unittest
from backend.product_images import thumbnail, enrich
from backend.business_reader import normalize
from backend.operations import normalize_operation
from backend.store import Store


class ProductImagesTests(unittest.TestCase):
    def test_only_product_image_fields_and_valid_urls(self):
        self.assertEqual(
            thumbnail(
                {"hdThumbUrl": "https://img.pddpic.com/main.jpg", "thumbUrl": "https://img.pddpic.com/small.jpg"}
            ),
            "https://img.pddpic.com/main.jpg",
        )
        self.assertEqual(thumbnail({"thumb_url": "//img.pddpic.com/a.jpg"}), "https://img.pddpic.com/a.jpg")
        self.assertEqual(
            thumbnail(
                {
                    "imageUrl": "javascript:alert(1)",
                    "pictures": ["https://example.com/review.jpg"],
                    "ev_certificate_img_url": "https://example.com/proof.jpg",
                }
            ),
            "",
        )
        self.assertEqual(thumbnail({"thumbnail_url": "https://username:secret@example.com/x"}), "")

    def test_catalog_never_matches_by_name_or_overrides_own_image(self):
        catalog = {"1": {"thumbnail_url": "https://img.pddpic.com/one.jpg", "name": "商品一"}}
        rows = enrich(
            [
                {"goods_id": "1"},
                {"goods_id": "2", "name": "商品一"},
                {"goods_id": "1", "thumbnail_url": "https://img.pddpic.com/snapshot.jpg"},
            ],
            catalog,
        )
        self.assertEqual(rows[0]["name"], "商品一")
        self.assertTrue(rows[0]["thumbnail_url"])
        self.assertEqual(rows[1]["thumbnail_url"], "")
        self.assertEqual(rows[2]["thumbnail_url"], "https://img.pddpic.com/snapshot.jpg")

    def test_base_modules_expose_uniform_field(self):
        fixtures = [
            ("products", {"id": 1, "hd_thumb_url": "https://img.pddpic.com/product.jpg"}),
            ("reviews", {"reviewId": 1, "thumbUrl": "https://img.pddpic.com/review-product.jpg"}),
            ("tickets", {"instanceId": 1, "thumbUrl": "https://img.pddpic.com/ticket-product.jpg"}),
        ]
        for kind, item in fixtures:
            with self.subTest(kind=kind):
                self.assertTrue(normalize(kind, item)["thumbnail_url"])

    def test_operations_return_image_including_every_intercept_parcel(self):
        fixtures = [
            ("invoices", {"order_sn": "o", "thumb_url": "https://img.pddpic.com/a.jpg"}),
            ("aftersales", {"id": 1, "orderSn": "o", "thumbUrl": "https://img.pddpic.com/a.jpg"}),
            (
                "evidence",
                {"afterSalesId": 1, "orderSn": "o", "goodsId": 99, "thumbUrl": "https://img.pddpic.com/a.jpg"},
            ),
            ("navigator", {"goodsId": 1, "hdThumbUrl": "https://img.pddpic.com/a.jpg"}),
            ("quality", {"goodsId": 1, "hdThumbUrl": "https://img.pddpic.com/a.jpg"}),
            ("brands", {"goodsId": 1, "imgUrl": "https://img.pddpic.com/a.jpg"}),
            (
                "intercepts",
                {
                    "goodsId": 1,
                    "orderSn": "o",
                    "thumbUrl": "https://img.pddpic.com/a.jpg",
                    "shipList": [{"shippingId": 1}, {"shippingId": 2}],
                },
            ),
        ]
        for kind, item in fixtures:
            with self.subTest(kind=kind):
                rows = normalize_operation(kind, item)
                self.assertTrue(all(r["thumbnail_url"] == "https://img.pddpic.com/a.jpg" for r in rows))
        self.assertEqual(len(rows), 2)

    def test_cache_catalog_isolates_shop_and_changed_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            a = store.add("first", "pw")
            b = store.add("second", "pw")
            for sid, name in ((a, "first"), (b, "second")):
                store.write_cache(
                    sid,
                    "products:fixture",
                    {"rows": [{"goods_id": "1", "name": name, "thumbnail_url": f"https://img.pddpic.com/{name}.jpg"}]},
                )
            self.assertEqual(store.product_images(a)["1"]["name"], "first")
            self.assertEqual(store.product_images(b)["1"]["name"], "second")
            store.update(a, "changed", "pw")
            self.assertEqual(store.product_images(a), {})
