import base64
from dataclasses import dataclass
import datetime
import json
import logging
import requests
import time
from typing import Any, Callable, Dict, Iterable, Optional


@dataclass
class SetFieldOperation:
    ado_field: str
    yt_field: str
    set_after_creation: bool


CustomFieldHandler = Callable[[Dict[str, Any]], Iterable[SetFieldOperation]]


class Migrator:
    def __init__(self, token_azdo, yt_base, ado_organization, ado_project, token_youtrack=None):
        self.yt_base = yt_base
        self.ado_base = f"{ado_organization}/{ado_project}/_apis/wit"
        self.auth_header_azdo = self._authorization_header_azdo(token_azdo)
        if(token_youtrack != None and token_youtrack != ""):
            self.auth_header_youtrack = self._authorization_header_youtrack(token_youtrack)
        else:
            self.auth_header_youtrack = None

    @staticmethod
    def _authorization_header_azdo(pat: str) -> str:
        return "Basic " + base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")

    @staticmethod
    def _authorization_header_youtrack(token: str) -> str:
        return "Bearer " + token

    @staticmethod
    def _set_field(ado_field: str, yt_field: str) -> Dict[str, Optional[str]]:
        return {
            "op": "add",
            "path": f"/fields/{ado_field}",
            "from": None,
            "value": yt_field,
        }

    @staticmethod
    def _format_yt_timestamp(timestamp: int) -> str:
        return datetime.datetime.utcfromtimestamp(timestamp // 1000).isoformat()

    def _youtrack_issue_data(self, yt_id: str):
        # We take the list of custom field keys from
        # https://www.jetbrains.com/help/youtrack/standalone/api-howto-get-issues-with-all-values.html
        yt_fields = (
            "customFields(name,value(avatarUrl,buildLink,color(id),fullName,id,"
            "isResolved,localizedName,login,minutes,name,presentation,text)),"
            "created,reporter(login),summary,description,"
            "comments(created,author(login),text),attachments(base64Content,name)"
        )
        yt_url = f"{self.yt_base}/api/issues/{yt_id}?fields={yt_fields}"
        headers = {}
        if(self.auth_header_youtrack != None):
            headers["Authorization"] = self.auth_header_youtrack
        yt_data = requests.get(yt_url, verify=False, headers=headers).json()
        return yt_data

    @staticmethod
    def _build_custom_field_dict(yt_data: Dict) -> Dict:
        return {v["name"]: v["value"] for v in yt_data["customFields"]}

    def custom_fields(self, yt_id: str) -> Dict:
        yt_data = self._youtrack_issue_data(yt_id)
        return self._build_custom_field_dict(yt_data)

    def migrate_issue(
        self,
        yt_id: str,
        custom_field_handler: CustomFieldHandler,
    ):
        create_ops = []  # Operations to perform on new Azure DevOps work item
        yt_data = self._youtrack_issue_data(yt_id)

        # Handle general information about issue
        summary = yt_data["summary"]
        create_ops.append(self._set_field("System.Title", summary))

        created = self._format_yt_timestamp(yt_data["created"])
        description = (
            f'[Migrated from <a href="{self.yt_base}/issue/{yt_id}">YouTrack</a>, '
            f'originally reported by {yt_data["reporter"]["login"]} on {created}]'
            f'\n\n{yt_data["description"]}'
        )
        description = description.replace("\n", "<br />\n")
        create_ops.append(self._set_field("System.Description", description))

        # Handle custom fields
        fields = self._build_custom_field_dict(yt_data)
        delayed_ops = []
        for custom_op in custom_field_handler(fields):
            op = self._set_field(custom_op.ado_field, custom_op.yt_field)
            (delayed_ops if custom_op.set_after_creation else create_ops).append(op)

        # Create new work item in Azure DevOps boards and get its ID
        # https://docs.microsoft.com/en-us/rest/api/azure/devops/wit/work%20items/create?view=azure-devops-rest-6.0
        res = requests.post(
            f"{self.ado_base}/workitems/$Task?api-version=6.0",
            headers={
                "Authorization": self.auth_header_azdo,
                "Content-Type": "application/json-patch+json",
            },
            json=create_ops,
        ).json()
        if "id" not in res:
            raise RuntimeError(f"migration of {yt_id} failed: {res}")
        work_item_id = res["id"]

        # Perform all operations that can only be performed after the work item has
        # been created
        requests.patch(
            f"{self.ado_base}/workitems/{work_item_id}?api-version=6.0",
            headers={
                "Authorization": self.auth_header_azdo,
                "Content-Type": "application/json-patch+json",
            },
            json=delayed_ops,
        )

        # Move all comments from YouTrack issue to the work item created above
        # https://docs.microsoft.com/en-us/rest/api/azure/devops/wit/comments/add?view=azure-devops-rest-6.0
        for comment in yt_data["comments"]:
            created = self._format_yt_timestamp(comment["created"])
            author = comment["author"]["login"]
            text = (
                f'[Migrated from <a href="{self.yt_base}/issue/{yt_id}">YouTrack</a>. '
                f"Original comment by {author} on {created}]"
                f'\n\n{comment["text"]}'
            )
            text = text.replace("\n", "<br/>\n")
            requests.post(
                f"{self.ado_base}/workItems/{work_item_id}"
                "/comments?api-version=6.0-preview.3",
                headers={
                    "Authorization": self.auth_header_azdo,
                    "Content-Type": "application/json",
                },
                json={"text": text},
            )

        # Move all attachments as well, keeping track of the file name used on YouTrack
        # https://docs.microsoft.com/en-us/rest/api/azure/devops/wit/attachments/create?view=azure-devops-rest-6.0
        # https://docs.microsoft.com/en-us/rest/api/azure/devops/wit/work%20items/update?view=azure-devops-rest-6.0#add-an-attachment
        for attachment in yt_data["attachments"]:
            name = attachment["name"]
            # We need to do this in two steps: First upload an attachment ...
            b64content = attachment["base64Content"].split(",")[1]
            decoded = base64.b64decode(b64content)
            res = requests.post(
                f"{self.ado_base}/attachments?api-version=6.0",
                headers={
                    "Authorization": self.auth_header_azdo,
                    "Content-Type": "application/octet-stream",
                },
                data=decoded,
            )

            # ... then take the URL of the newly created attachment and add that to
            # the work item
            attachment_url = res.json()["url"]
            attachment_data = [
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "AttachedFile",
                        "url": attachment_url,
                        "attributes": {"name": name},
                    },
                }
            ]
            requests.patch(
                f"{self.ado_base}/workItems/{work_item_id}?api-version=6.0",
                headers={
                    "Authorization": self.auth_header_azdo,
                    "Content-Type": "application/json-patch+json",
                },
                json=attachment_data,
            )

    def migrate_project(
        self,
        yt_project: str,
        custom_field_handler: CustomFieldHandler,
        issue_count_upper_limit: int = 10000,
    ):
        headers = {}
        if(self.auth_header_youtrack != None):
            headers["Authorization"] = self.auth_header_youtrack
        issues = requests.get(
            f"{self.yt_base}/api/issues?fields=idReadable"
            f"&$top={issue_count_upper_limit}"
            f"&query=project:+{yt_project}",
            verify=False,
            headers=headers
        ).json()
        for i, issue in enumerate(issues):
            yt_id = issue["idReadable"]
            logging.info(f"Migrating {yt_id}, {i + 1}/{len(issues)}")
            # When handling large migrations, the Azure DevOps will occasionally return
            # empty responses. We handle this by retrying after a while.
            while True:
                try:
                    self.migrate_issue(yt_id, custom_field_handler)
                    break
                except json.decoder.JSONDecodeError:
                    logging.info("Encountered an error. Wait 3 seconds")
                    time.sleep(3)
            logging.info(f"Migrated {yt_id}, {i + 1}/{len(issues)}")
