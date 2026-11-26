"""إعداد النشر على فيسبوك (مرة واحدة).
بتشغله في Termux، بيطلب منك: App ID و App Secret والتوكن المؤقت (بتلصقهم هنا بس، مش في الشات).
بيطلّع مفتاح الصفحة الدائم ويحفظه في ~/env.sh (FB_PAGE_ID و FB_PAGE_TOKEN).
"""
import os, json, getpass, urllib.request, urllib.parse

V = "v21.0"


def get(path, **params):
    url = f"https://graph.facebook.com/{V}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print("خطأ من فيسبوك:", e.read().decode()[:400])
        raise SystemExit(1)


def main():
    app_id = input("App ID: ").strip()
    secret = getpass.getpass("App Secret (مش هيظهر وانت بتلصقه): ").strip()
    short = getpass.getpass("التوكن المؤقت (مش هيظهر): ").strip()
    ll = get("oauth/access_token", grant_type="fb_exchange_token", client_id=app_id,
             client_secret=secret, fb_exchange_token=short)["access_token"]
    pages = get("me/accounts", access_token=ll).get("data", [])
    if not pages:
        print("ملقيتش صفحات. اتأكد إنك اخترت الصفحة وأذنت بالصلاحيات.")
        return
    for i, p in enumerate(pages, 1):
        print(f"{i}) {p['name']}")
    n = int(input("رقم الصفحة: ").strip()) - 1
    page = pages[n]
    path = os.path.expanduser("~/env.sh")
    lines = []
    if os.path.exists(path):
        lines = [l for l in open(path, encoding="utf-8").read().splitlines()
                 if not l.startswith(("export FB_PAGE_ID=", "export FB_PAGE_TOKEN="))]
    lines += [f"export FB_PAGE_ID={page['id']}", f"export FB_PAGE_TOKEN={page['access_token']}"]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("تمام ✅ اتحفظت صفحة:", page["name"])


main()
