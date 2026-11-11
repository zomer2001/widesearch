"""One real Scrapy reactor per task; fixed same-domain breadth-first crawling."""
import argparse
import json
from urllib.parse import urlsplit

import scrapy
from scrapy.crawler import CrawlerProcess

from ..common import atomic_json, read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    request = read_json(args.input)
    pages, visits = [], []
    max_pages = request["max_pages"]

    class FixedSpider(scrapy.Spider):
        name = "widesearch_fixed"
        allowed_domains = sorted({urlsplit(u).hostname for u in request["urls"]})
        scheduled = set()

        async def start(self):
            for url in request["urls"]:
                if len(self.scheduled) >= max_pages:
                    break
                self.scheduled.add(url)
                yield scrapy.Request(url, callback=self.parse, errback=self.failed)

        def failed(self, failure):
            visits.append({"url": failure.request.url, "success": False, "error": type(failure.value).__name__})
            atomic_json(args.output, {"pages": pages, "visits": visits})

        def parse(self, response):
            import trafilatura
            text = trafilatura.extract(response.body, include_tables=True, include_links=True) or ""
            visits.append({"url": response.url, "success": bool(text) and response.status == 200, "status": response.status})
            if text:
                pages.append({"url": response.url, "content": text})
            atomic_json(args.output, {"pages": pages, "visits": visits})
            if response.meta.get("depth", 0) >= request["max_depth"]:
                return
            for href in response.css("a::attr(href)").getall():
                url = response.urljoin(href).split("#")[0]
                if urlsplit(url).scheme not in {"http", "https"} or urlsplit(url).hostname not in self.allowed_domains:
                    continue
                if url in self.scheduled or len(self.scheduled) >= max_pages:
                    continue
                self.scheduled.add(url)
                yield response.follow(url, callback=self.parse, errback=self.failed)

    process = CrawlerProcess({"LOG_LEVEL": "ERROR", "ROBOTSTXT_OBEY": True, "CONCURRENT_REQUESTS": 3,
                              "DOWNLOAD_TIMEOUT": 30, "RETRY_TIMES": 1, "DEPTH_LIMIT": request["max_depth"],
                              "DEPTH_PRIORITY": 1, "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
                              "USER_AGENT": "Mozilla/5.0 (compatible; WideSearchResearch/1.0)"})
    process.crawl(FixedSpider)
    process.start()
    atomic_json(args.output, {"pages": pages, "visits": visits})


if __name__ == "__main__":
    main()
