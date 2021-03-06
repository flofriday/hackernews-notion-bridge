from dataclasses import dataclass
from typing import Any, List, Optional, Union
from datetime import datetime
from notion_client import Client as NotionClient, APIResponseError
from bs4 import BeautifulSoup
import bs4
import json
import html
import re
import httpx
import asyncio
import logging
import argparse

TIMEOUT = 60  # The timeout is so high because we do many requests in paraleell


class DownloadExcpetion(Exception):
    pass


@dataclass
class Comment:
    """A Hackernews Comment"""

    by: str
    id: int
    comments: List["Comment"]
    text: BeautifulSoup
    time: datetime


@dataclass
class Story:
    """A Hackernews Story"""

    by: Optional[str]
    id: int
    comments: List[Comment]
    score: int
    time: datetime
    title: str
    url: str


def process_comment_html(text: str) -> str:
    """
    Dirty hack to process hackernews html into plain text.

    This function should be replaced by proper html parsing and converting
    them to the corresponding notion rich text elements. However, for now
    this is better than nothing.

    This also only works since, hackernews comments (to my knowledge) only
    contain <p>, <a> and <i> tags.

    The <p> tag gets replaced by two newlines, but all other tags get replaced
    by their text.
    """
    # TODO: do this properly
    text = re.sub(r"<p>", "\n\n", text)
    text = re.sub(r"<.*>", "", text)
    text = html.unescape(text)
    text = text.strip()
    return text


async def download_comment(id: int, client: httpx.AsyncClient) -> Comment:
    """Download a HN comment and all of its comments recursively"""
    # Download the comment
    try:
        r = await client.get(
            f"https://hacker-news.firebaseio.com/v0/item/{id}.json",
            timeout=TIMEOUT,
        )
    except httpx.TimeoutException as e:
        raise DownloadExcpetion(f"Timeout downloading comment {id}") from e

    if r.status_code != 200:
        raise DownloadExcpetion(
            f"Hackernews API returned status: {r.status_code}"
        )

    data = r.json()
    if data is None or "deleted" in data and data["deleted"]:
        raise DownloadExcpetion(f"Comment {id} is deleted")

    # First download the child-comments recursively:
    if "kids" in data:
        comment_ids = data["kids"]
        comments = await asyncio.gather(
            *map(lambda c: download_comment(c, client), comment_ids)
        )
        comments = [c for c in comments if c is not None]
    else:
        comments = []

    # Now create the comment
    return Comment(
        by=data["by"],
        id=data["id"],
        comments=comments,
        text=BeautifulSoup(data["text"], "html.parser"),
        time=datetime.fromtimestamp(data["time"]),
    )


async def download_story(id: int, client: httpx.AsyncClient) -> Story:
    """Download a HN story and all of its comments"""
    # Download the story
    try:
        r = await client.get(
            f"https://hacker-news.firebaseio.com/v0/item/{id}.json",
            timeout=TIMEOUT,
        )
    except httpx.TimeoutException as e:
        raise DownloadExcpetion(f"Timeout downloading story {id}") from e

    if r.status_code != 200:
        raise DownloadExcpetion(
            f"Hackernews API returned {r.status_code} downloading story {id}"
        )

    data = r.json()

    # Ignore stories without an url for now
    # TODO: fix that in the future
    if "url" not in data:
        raise DownloadExcpetion(f"Story {id} has no URL")

    # First download the child-comments recursively:
    comments = []
    if "kids" in data:
        comment_ids = data["kids"]
        comments_raw = await asyncio.gather(
            *map(lambda c: download_comment(c, client), comment_ids),
            return_exceptions=True,
        )
        for comment in comments_raw:
            if isinstance(comment, DownloadExcpetion):
                logging.warning(
                    f"{str(comment)}\nSkipped comment {comment.args[0]}"
                )
                continue
            comments.append(comment)

    # Now create the story
    return Story(
        by=data["by"] if "by" in data else None,
        id=data["id"],
        comments=comments,
        score=data["score"],
        time=datetime.fromtimestamp(data["time"]),
        title=data["title"],
        url=data["url"],
    )


def count_comments(item: Union[Comment, Story]) -> int:
    """Recursively count all comments"""
    result = 0
    for comment in item.comments:
        result += count_comments(comment)

    result += len(item.comments)
    return result


async def download_stories(number: int) -> List[Story]:
    """Download the first n top HN stories"""
    # First download which stories are currently trending
    r = httpx.get("https://hacker-news.firebaseio.com/v0/topstories.json")

    if r.status_code != 200:
        raise Exception(f"Error downloading stories: {r.status}")

    top_story_ids = r.json()

    # Now download the stories
    # Only download one story (with its comments) at a time as I got timouts
    # without
    async with httpx.AsyncClient() as client:
        stories = []
        index = 0
        while len(stories) < number:
            story_id = top_story_ids[index]
            index += 1
            logging.info(
                f"Downloading story [{len(stories) + 1} of {number}]: "
                f"{story_id}"
            )
            try:
                story = await download_story(story_id, client)
            except DownloadExcpetion as e:
                logging.warning(f"{str(e)}\nSkipped story {story_id}")
                continue
            stories.append(story)

    return stories


def properties_from_story(story: Story, position: int) -> dict:
    """Create Notion properties from a story"""
    return {
        "Title": {
            "title": [
                {"text": {"content": story.title}},
            ],
        },
        "Position": {"number": position},
        "Website": {"url": story.url},
        "Hackernews Link": {
            "url": f"https://news.ycombinator.com/item?id={story.id}"
        },
        "Comments": {"number": count_comments(story)},
        "Points": {"number": story.score},
    }


def richtexts_from_html(tag: Any, style=None) -> List[dict]:
    """Create notion richtexts from a html soup"""
    if style is None:
        style = dict()
    else:
        style = style.copy()

    if isinstance(tag, bs4.element.NavigableString):
        obj = {
            "type": "text",
            "text": {
                # Notion only allows up to 2000 characters in a richtext
                "content": tag.string[: (2000 - 2)],
            },
            "annotations": {},
        }

        if "bold" in style and style["bold"]:
            obj["annotations"]["bold"] = True
        if "italic" in style and style["italic"]:
            obj["annotations"]["italic"] = True
        if "code" in style and style["code"]:
            obj["annotations"]["code"] = True
        if "color" in style and style["color"]:
            obj["annotations"]["color"] = style["color"]
        if "url" in style:
            obj["text"]["link"] = dict()
            obj["text"]["link"]["url"] = style["url"]
        return [obj]

    res = []
    if tag.name == "i":
        style["italic"] = True

    if tag.name == "b":
        style["bold"] = True

    if tag.name in ["code", "pre"]:
        style["code"] = True

    if tag.name == "a":
        style["url"] = tag["href"]
        style["color"] = "blue"

    for kid in tag.contents:
        res += richtexts_from_html(kid, style)

    if tag.name == "p":
        res[0]["text"]["content"] = "\n\n" + res[0]["text"]["content"]

    if tag.name not in ["i", "b", "a", "p", "pre", "code", "[document]"]:
        logging.warning(f"Unknown tag found: {tag.name}")

    return res


def block_from_comment(comment: Comment) -> dict:
    """Create Notion a block from a comment"""

    # Convert the html comment to notion richtext
    try:
        text = richtexts_from_html(comment.text)
    except Exception as e:
        logging.warning(
            f"Error parsing richtext comment {comment.id}: {repr(e)}"
        )
        text = [
            {
                "type": "text",
                "text": {
                    "content": comment.text.getText()[:2000],
                },
            }
        ]

    # Add the author's name
    text.extend(
        [
            {
                "type": "text",
                "text": {
                    "content": "\nby ",
                },
                "annotations": {
                    "color": "gray",
                },
            },
            {
                "type": "text",
                "text": {
                    "content": comment.by,
                    "link": {
                        "url": f"https://news.ycombinator.com/user?id={comment.by}",
                    },
                },
                "annotations": {
                    "color": "gray",
                },
            },
        ]
    )

    # Assemble the final block
    block = {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"text": text},
    }

    if len(comment.comments) != 0:
        children = [block_from_comment(c) for c in comment.comments]
        block["bulleted_list_item"]["children"] = children

    return block


def blocks_from_story(story: Story) -> list:
    """Create Notion blocks from a story"""
    blocks = [
        # Link previewcard to click
        {
            "object": "block",
            "type": "bookmark",
            "bookmark": {"url": story.url},
        },
        # Meta information about the story
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "by ",
                        },
                    },
                    {
                        "type": "text",
                        "text": {
                            "content": story.by,
                            "link": {
                                "url": "https://news.ycombinator.com/user?"
                                f"id={story.by}",
                            },
                        },
                    },
                ],
            },
        },
        # Comment header
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "Comments",
                        },
                    }
                ],
            },
        },
    ]

    # Add comments if there are any
    if len(story.comments) != 0:
        for comment in story.comments:
            blocks.append(block_from_comment(comment))
    else:
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "text": [
                        {
                            "type": "text",
                            "text": {
                                "content": "There are no comments yet.",
                            },
                        },
                    ]
                },
            }
        )

    return blocks


def update_notion(stories: List[Story]):
    """Upload stories to notion and remove old ones"""
    # Load the config
    config = json.load(open("config.json"))
    database_id = config["database"]
    token = config["token"]
    notion = NotionClient(auth=token)

    # Delete all old stories
    # TODO: instead of delete, maybe just update their content
    kids = notion.databases.query(database_id)["results"]
    logging.info(f"Deleting {len(kids)} old stories")
    for page in kids:
        notion.pages.update(page["id"], archived=True)

    # Add the new stories
    for n, story in enumerate(stories):
        logging.info(f"Uploading story [{n+1} of {len(stories)}]: {story.id}")
        properties = properties_from_story(story, n + 1)
        blocks = blocks_from_story(story)
        try:
            notion.pages.create(
                parent={"database_id": database_id},
                properties=properties,
                children=blocks,
            )
        except APIResponseError as e:
            logging.error(f"Error uploading story {story.id}: {e}")


async def main():
    # Setup logging
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
        level=logging.INFO,
    )

    # Setup argparse
    parser = argparse.ArgumentParser(description="Hackernews-Notion-Bridge")
    parser.add_argument(
        "--loop",
        help="Update every 10min, instead of just running once",
        action="store_true",
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=10,
        help="Number of stories to download (default:10).",
    )
    args = parser.parse_args()

    # Download and re-upload stories
    stories = await download_stories(args.number)
    update_notion(stories)

    # Loop if specified
    if args.loop:
        while True:
            logging.info("Sleeping 10min ...")
            await asyncio.sleep(10 * 60)
            stories = await download_stories()
            update_notion(stories)


if __name__ == "__main__":
    asyncio.run(main())
