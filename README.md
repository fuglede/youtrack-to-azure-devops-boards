# YouTrack to Azure DevOps issue migration

This project contains a script for migrating issues from YouTrack to Azure DevOps
Boards. It can migrate issues from a given YouTrack project to a given Azure DevOps
project. It supports migration of comments, attachments, and custom fields.


## Example usage

All interfacing with Azure DevOps requires a [personal access token](https://docs.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate?view=azure-devops)
granting read+write access to work items, and which we refer to as `pat` below:

```python
from migrator import Migrator, SetFieldOperation

pat = 'wz13hy65okghxfuodbvk3tmkilefxnx6gret04sl1m0didiocept'  # Placeholder: Replace with your own PAT
yt_base = "https://my-youtrack-instance"
ado_organization = "https://dev.azure.com/organization-name"
ado_project = "project-name"
migrator = Migrator(pat, yt_base, ado_organization, ado_project)
```

With this, you may want to first get a list of available custom fields on a given
YouTrack issue, say `AB-123`:

```python
print(migrator.custom_fields('AB-123'))
```

These custom fields can then be used to define how to migrate a particular issue or
YouTrack project; for instance, to move YouTrack fields `'Priority'` and `'Estimation'`
to Azure DevOps fields `'Task Priority'`, `'Estimation'`, define a handler through

```python
def custom_field_handler(fields):
    priority = fields["Priority"].get("name", "")
    yield SetFieldOperation("Task Priority", priority, False)

    estimation = fields["Estimation"].get("presentation", "")
    yield SetFieldOperation("Estimation", estimation, False)
```

Here, the keys `'name'` and `'presentation'` depend on the type of custom field and
can be read from the output of `migrator.custom_fields` above.

Note that when using "Task" as work item type in Azure DevOps Boards, some fields, such
as `System.State` and `System.Reason` can only be set after the work item has been
created; for such fields, use `True` for the third parameter above.

With this, we can migrate a particular issue through

```python
migrator.migrate_issue('AB-123', custom_field_handler)
```

or an entire project through
```python
migrator.migrate_project('AB', custom_field_handler, issue_count_upper_limit=50000)
```

where here, `number_of_issues` is simply any number greater than the number of issues
in the project.