from dataclasses import dataclass
from typing import List, Optional, Union
from datetime import datetime
from notion_client import Client as NotionClient
import json
import html
import re
import httpx
import asyncio
import logging
import argparse

NUM_STORIES = 30
TIMEOUT = 60  # The timeout is so high because we do many requests in paraleell


@dataclass
class Comment:
    """A Hackernews Comment"""

    by: str
    id: int
    comments: List["Comment"]
    text: str
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


async def download_comment(
    id: int, client: httpx.AsyncClient
) -> Optional[Comment]:
    """Download a HN comment and all of its comments recurisvly"""
    # Download the comment
    r = await client.get(
        f"https://hacker-news.firebaseio.com/v0/item/{id}.json",
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        logging.warning(f"Hackernews API returned {r.status_code}")
        return None

    data = r.json()
    if data is None or "deleted" in data and data["deleted"]:
        return None

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
        text=process_comment_html(data["text"]),
        time=datetime.fromtimestamp(data["time"]),
    )


async def download_story(id: int, client: httpx.AsyncClient) -> Story:
    """Download a HN story and all of its comments"""
    # Download the story
    r = await client.get(
        f"https://hacker-news.firebaseio.com/v0/item/{id}.json",
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        logging.warning(f"Hackernews API returned {r.status_code}")
        return None

    data = r.json()

    # Ignore soties without an url for now
    # TODO: fix that in the future
    if "url" not in data:
        logging.warning(f"Story {id} has no URL")
        return None

    # First download the child-comments recursively:
    if "kids" in data:
        comment_ids = data["kids"]
        comments = await asyncio.gather(
            *map(lambda c: download_comment(c, client), comment_ids)
        )
        comments = [c for c in comments if c is not None]
    else:
        comments = []

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


async def download_stories() -> List[Story]:
    """Download the first n top HN stories"""
    # First downnload which stories are currently trending
    r = httpx.get("https://hacker-news.firebaseio.com/v0/topstories.json")

    if r.status_code != 200:
        raise Exception(f"Error downloading stories: {r.status}")

    top_stories = r.json()[: NUM_STORIES + 1]

    # Now download the stories
    # Only download one story (with its comments) at a time as I got timouts
    # without
    async with httpx.AsyncClient() as client:
        stories = []
        index = 0
        while len(stories) < NUM_STORIES:
            story_id = top_stories[index]
            index += 1
            story = await download_story(story_id, client)
            if story is None:
                logging.warning(f"Skipped story {story_id}")
                continue
            stories.append(story)
            logging.info(f"Downloaded story {story_id}")

    return stories


def properties_from_story(story: Story, position: int) -> dict:
    """Create Notion properties from a story"""
    return {
        "Title": {
            "title": [
                {"text": {"content": story.title}},
            ],
        },
        "Pos": {"number": position},
        "Website": {"url": story.url},
    }


def block_from_comment(comment: Comment) -> dict:
    """Create Notion a block from a comment"""
    block = {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "text": [
                {
                    # Notion only allows up to 2000 characters
                    "text": {"content": comment.text[:2000]},
                },
            ],
        },
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
                            "content": f"{story.score} Points · "
                            f"{count_comments(story)} Comments · "
                            f"by {story.by}",
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

    return blocks


def update_notion(stories: List[Story]):
    """Upload stories to notion and remove old ones"""
    # Load the config
    config = json.load(open("config.json"))
    database_id = config["database"]
    token = config["token"]
    notion = NotionClient(auth=token)

    # Delete all old stories
    # TODO: instead of delelte, maybe just update their content
    kids = notion.databases.query(database_id)["results"]
    for page in kids:
        notion.pages.update(page["id"], archived=True)
    logging.info(f"Deleted {len(kids)} old stories")

    # Add the new stories
    for n, story in enumerate(stories):
        properties = properties_from_story(story, n + 1)
        blocks = blocks_from_story(story)
        notion.pages.create(
            parent={"database_id": database_id},
            properties=properties,
            children=blocks,
        )
        logging.info(f"Uploaded story {story.id}")


async def main():
    # Setup logging
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
        level=logging.INFO,
    )

    # Setup argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loop",
        help="Update every 10min, instead of just running once",
        action="store_true",
    )
    args = parser.parse_args()

    # Download and reupload stories
    stories = await download_stories()
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
