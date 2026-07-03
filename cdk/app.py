import os

from aws_cdk import App

from stacks.lambda_cron_stack import LambdaCronStack
from stacks.lambda_api_stack import LambdaApiStack

app = App()

deploy_type = app.node.try_get_context("deploy_type")
function_name = app.node.try_get_context("function_name")
image_uri = app.node.try_get_context("image_uri")
memory_size = int(app.node.try_get_context("memory_size") or 512)
timeout_seconds = int(app.node.try_get_context("timeout_seconds") or 60)

env = {
    "account": os.environ.get("CDK_DEFAULT_ACCOUNT"),
    "region": os.environ.get("CDK_DEFAULT_REGION"),
}

if deploy_type == "cron":
    schedule_expression = app.node.try_get_context("schedule_expression")
    LambdaCronStack(
        app, f"{function_name}-cron-stack",
        function_name=function_name,
        image_uri=image_uri,
        schedule_expression=schedule_expression,
        memory_size=memory_size,
        timeout_seconds=timeout_seconds,
        env=env,
    )
elif deploy_type == "api":
    LambdaApiStack(
        app, f"{function_name}-api-stack",
        function_name=function_name,
        image_uri=image_uri,
        memory_size=memory_size,
        timeout_seconds=timeout_seconds,
        env=env,
    )
else:
    # Allow `cdk synth`/`cdk list` to run without context for sanity checks
    pass

app.synth()
