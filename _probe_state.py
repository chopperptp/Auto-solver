"""ตรวจสอบ: คำตอบที่ 'ถูกบันทึก' ตรงกับที่ AI เลือกหรือไม่ (สงสัยคลิกผิดช่อง)"""
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = ("https://learn.thaimooc.ac.th/xblock/"
       "block-v1:TU+00012+0+type@vertical+block@6cf7c854af39408d8d383b8e74b730eb")
PROFILE = str(Path(__file__).parent / "browser_profile")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=120000)
    time.sleep(5)

    data = page.evaluate(
        """() => [...document.querySelectorAll('.problem')].map((p,i) => {
            const radios = [...p.querySelectorAll('input[type=radio]')];
            return {
                i,
                legend: (p.querySelector('legend')||{}).innerText || '',
                status: (p.querySelector('.status')||{}).className || '',
                options: radios.map(r => {
                    const lab = p.querySelector('label[for="'+r.id+'"]');
                    return r.value + ' | ' + (lab ? lab.innerText.replace(/\\s+/g,' ').trim().slice(0,45) : '?');
                }),
                checkedText: radios.filter(r=>r.checked).map(r => {
                    const lab = p.querySelector('label[for="'+r.id+'"]');
                    return r.value + ' | ' + (lab ? lab.innerText.replace(/\\s+/g,' ').trim().slice(0,45) : '?');
                }),
                correctText: [...p.querySelectorAll('label.choicegroup_correct')].map(l=>l.innerText.replace(/\\s+/g,' ').trim().slice(0,45)),
            };
        })"""
    )
    print(json.dumps(data, ensure_ascii=False, indent=1))
    ctx.close()
