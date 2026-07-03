from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_lambda as _lambda,
    aws_ecr as ecr,
    aws_events as events,
    aws_events_targets as targets,
)
from constructs import Construct


def parse_image_uri(image_uri: str):
    """
    '123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo:abc123'
      -> ('my-repo', 'abc123')
    """
    repo_and_tag = image_uri.split("/")[-1]
    if ":" in repo_and_tag:
        repo_name, tag = repo_and_tag.split(":", 1)
    else:
        repo_name, tag = repo_and_tag, "latest"
    return repo_name, tag


class LambdaCronStack(Stack):
    def __init__(
            self,
            scope: Construct,
            construct_id: str,
            *,
            function_name: str,
            image_uri: str,
            schedule_expression: str,
            memory_size: int = 512,
            timeout_seconds: int = 60,
            environment: dict = None,
            **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repo_name, tag = parse_image_uri(image_uri)
        repository = ecr.Repository.from_repository_name(self, "Repo", repo_name)

        fn = _lambda.DockerImageFunction(
            self,
            "CronFunction",
            function_name=function_name,
            code=_lambda.DockerImageCode.from_ecr(repository=repository, tag_or_digest=tag),
            timeout=Duration.seconds(timeout_seconds),
            memory_size=memory_size,
            environment=environment or {},
        )

        rule = events.Rule(
            self,
            "ScheduleRule",
            schedule=events.Schedule.expression(schedule_expression),
        )
        rule.add_target(targets.LambdaFunction(fn))

        CfnOutput(self, "FunctionArn", value=fn.function_arn)
        CfnOutput(self, "FunctionName", value=fn.function_name)
        CfnOutput(self, "ScheduleRuleArn", value=rule.rule_arn)