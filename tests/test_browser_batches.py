"""Opt-in real-browser batch smoke: REDLINING_BROWSER_TESTS=1 pytest this file."""
import os
import pytest


@pytest.mark.skipif(os.environ.get('REDLINING_BROWSER_TESTS')!='1', reason='Opt-in Selenium/PostgreSQL stack')
def test_two_drafts_commit_atomically_and_other_browser_converges(tmp_path):
    from benchmarks.run import Stack
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    with Stack(tmp_path,None) as stack:
        stack.store.create_document('batch-ui','First ALPHA; second BETA; final unchanged.','Batch UI')
        stack.workers()
        options=webdriver.ChromeOptions();options.add_argument('--headless=new')
        first=webdriver.Chrome(options=options)
        second=None
        try:
            second=webdriver.Chrome(service=Service(first.service.path),options=options)
            for driver in (first,second):
                driver.get(stack.ui)
                WebDriverWait(driver,20).until(lambda d:d.find_element(By.CSS_SELECTOR,'.revision').text=='v1' and not d.find_elements(By.CSS_SELECTOR,'.connection'))
            for token,new in [('ALPHA','longer first'),('BETA','short')]:
                first.execute_script('''const token=arguments[0];
                    const el=[...document.querySelectorAll('.source [data-start]')].find(e=>e.textContent.includes(token));
                    const pos=el.textContent.indexOf(token);const range=document.createRange();
                    range.setStart(el.firstChild,pos);range.setEnd(el.firstChild,pos+token.length);
                    getSelection().removeAllRanges();getSelection().addRange(range);
                    el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));''',token)
                wait=WebDriverWait(first,15)
                editor=wait.until(lambda d:next((e for e in d.find_elements(By.CSS_SELECTOR,'[data-suggestion][contenteditable="plaintext-only"]') if not e.text),None))
                editor.send_keys(new)
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,'button.submit')))
            assert first.find_element(By.CSS_SELECTOR,'button.submit').text=='Submit edits (2)'
            first.find_element(By.CSS_SELECTOR,'button.submit').click()
            expected='First longer first; second short; final unchanged.'
            for driver in (first,second):
                WebDriverWait(driver,20).until(lambda d:d.find_element(By.CSS_SELECTOR,'.revision').text=='v2')
                assert driver.execute_script("return [...document.querySelectorAll('.source [data-start]')].map(e=>e.textContent).join('')")==expected
            with stack.store.connection() as db:
                assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==1
                assert db.execute('SELECT count(*) AS n FROM replacement_items').fetchone()['n']==2
            first.save_screenshot(str(tmp_path/'batch-ui.png'))
        finally:
            if second:second.quit()
            first.quit()
