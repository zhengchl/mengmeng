# -*- coding: utf-8 -*-
import collections
import aiohttp
import asyncio
import logging
import time
import re

from bs4 import BeautifulSoup

# 爬取前MAX_PAGE页
MAX_PAGE = 100

NUM_PRODUCERS = 8
NUM_HTML_CONSUMERS = NUM_PRODUCERS * 2

# 证监会公示网页
BASE_URL = 'https://neris.csrc.gov.cn/alappl/home/volunteerLift.do'

HEADERS_STR = '''
    Host: neris.csrc.gov.cn
    Connection: keep-alive
    sec-ch-ua: "Chromium";v="92", " Not A;Brand";v="99", "Google Chrome";v="92"
    sec-ch-ua-mobile: ?0
    Upgrade-Insecure-Requests: 1
    User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36
    Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9
    Sec-Fetch-Site: same-origin
    Sec-Fetch-Mode: navigate
    Sec-Fetch-User: ?1
    Sec-Fetch-Dest: iframe
    Referer: https://neris.csrc.gov.cn/alappl/home/volunteerLift?edCde=300009
    Accept-Encoding: gzip, deflate, br
    Accept-Language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7
    Cookie: JSESSIONID=2E39879D44E93612666AA83F17A8C883; fromDetail=false
'''
HEADERS = dict([[h.partition(':')[0].strip(), h.partition(':')[2].strip()]
                for h in HEADERS_STR.split('\n')])

DATE_FORMAT_RO = re.compile(r'\d\d\d\d-\d\d-\d\d')

async def featch_data(pn: int, session: aiohttp.ClientSession) -> str:
    paras_dict = {'edCde':'300009', 'pageNo':str(pn), 'pageSize':'10'}
    async with session.get(BASE_URL, params = paras_dict, headers = HEADERS) as resp:
        return await resp.text()

async def producer(name: str,
                   page_range: range,
                   html_q: asyncio.Queue,
                   session: aiohttp.ClientSession) -> None:
    logging.info(f"Producer {name} inited," +
                 f" fetch page {page_range.start} -> {page_range.stop-1}.")
    for pn in page_range:
        html = await featch_data(pn, session)
        logging.info(f"Producer {name} fetch page {pn}")
        await html_q.put((pn, html))
    logging.info(f"Producer {name} finished.")

async def html_consumer(name: str,
                        html_q: asyncio.Queue,
                        data: dict) -> None:
    logging.info(f"Html consumer {name} inited.")
    while True:
        pn, html = await html_q.get()
        contents = parse_data(html)
        logging.info(f"Html consumer {name} parse page {pn}, get {len(contents)} lines")
        data[pn] = contents
        html_q.task_done()

def write_out(data: dict) -> None:
    valid_action = collections.OrderedDict()
    valid_action["接收材料"] = ""
    valid_action["补正通知"] = ""
    valid_action["接收补正材料"] = ""
    valid_action["受理通知"] = ""
    valid_action["一次书面反馈"] = ""
    valid_action["接收书面回复"] = ""
    valid_action["行政许可决定书"] = ""
    valid_action["二次书面反馈"] = ""
    valid_action["一次中止审查通知"] = ""
    valid_action["申请人主动撤销"] = ""
    valid_action["终止审查通知"] = ""
    with open('机构公示.txt', "w", encoding='utf-8') as ofid:
        ofid.write('%s|%s|%s\n'%('页码', '标题', "|".join(valid_action.keys())))
        for pn in range(1, MAX_PAGE + 1):
            if pn not in data:
                logging.warning(f"Page {pn} not in write out data.")
                continue
            logging.info(f"Write out page {pn}.")
            for title, _, table in data[pn]:
                table_content_dict = valid_action.copy()
                for table_content in table:
                    table_content_dict[table_content[0]] = table_content[1]
                ofid.write('%d|%s|%s\n'%(pn, title, "|".join(table_content_dict.values())))

async def main(prod_num: int, html_con_num: int):
    data = dict()
    async with aiohttp.ClientSession() as session:
        html_q = asyncio.Queue()
        prod_num = min(prod_num, MAX_PAGE)
        producer_list = [asyncio.create_task(producer(idx, page_range, html_q, session))
                         for idx, page_range in divide_page(prod_num)]
        html_consumer_list = [asyncio.create_task(html_consumer(idx, html_q, data))
                              for idx in range(html_con_num)]
        await asyncio.gather(*producer_list)
        await html_q.join()
        for con in html_consumer_list:
            con.cancel()
    write_out(data)

def divide_page(prod_num):
    size = MAX_PAGE // prod_num
    for i in range(prod_num):
        left = i * size + 1
        right = 1 + (MAX_PAGE if i + 1 == prod_num else (i + 1) * size)
        yield (i, range(left, right))

def get_children_number(tag):
    '''获取tag的子tag数目'''
    number = 0
    for child in tag.children:
        if not hasattr(child, "text"):
            continue
        number += 1
    return number

def get_deep_text(tag):
    '''获取tag最深一层的text'''
    cur_tag = tag
    cur_tag_number = get_children_number(cur_tag)
    while get_children_number(cur_tag) != 0:
        if cur_tag_number == 1:
            for child in cur_tag.children:
                if not hasattr(child, "text"):
                    continue
                else:
                    cur_tag = child
                    cur_tag_number = get_children_number(cur_tag)
        else:
            return "find_more_than_one_tag"
    return cur_tag.text.strip()

def parse_data(html: str):
    soup = BeautifulSoup(html, "lxml")
    titles = soup.find_all("div", {"class":"titleshow"})

    contents = []
    for title in titles:
        title_text = title.text.strip()
        title_date = ""
        table_content = []
        cur_tag = title
        for i in range(7): # 最多向后检查7个sibling，检查是否有时间串
            cur_tag = cur_tag.next_sibling # 这里有坑，'\n'也算一个sibling
            if not hasattr(cur_tag, "text"):
                continue
            date_search = DATE_FORMAT_RO.search(cur_tag.text)
            if date_search and title_date == "":
                title_date = date_search.group()
            
            if cur_tag.name == "table" and len(table_content) == 0:
                # table的第一行为“进度追踪”，第二行为标题行“任务名称 | 完成时间”，因此从第三行开始解析
                # table的每一行有两列
                table_cur_line = 0
                for table_tag in cur_tag.children:
                    if not hasattr(table_tag, "text"):
                        continue
                    table_cur_line += 1
                    if table_cur_line < 3:
                        continue
                    td_tags = table_tag.find_all("td")
                    if len(td_tags) != 2:
                        continue
                    table_content.append((get_deep_text(td_tags[0]), get_deep_text(td_tags[1])))
        
        contents.append((title_text, title_date, table_content))
    return contents

if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.DEBUG)
    
    logging.info(f"Program begin, crawl {MAX_PAGE} pages, " +
                 f"with {NUM_PRODUCERS} producers and {NUM_HTML_CONSUMERS} consumers")
    start = time.perf_counter()
    asyncio.run(main(NUM_PRODUCERS, NUM_HTML_CONSUMERS))
    elapsed = time.perf_counter() - start
    logging.info(f"Program completed in {elapsed:0.5f} seconds.")