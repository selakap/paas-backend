from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_lambda as _lambda,
    aws_ecr as ecr,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
)
from constructs import Construct

from stacks.lambda_cron_stack import parse_image_uri


class LambdaApiStack(Stack):
    def __init__(
            self,
            scope: Construct,
            construct_id: str,
            *,
            function_name: str,
            image_uri: str,
            memory_size: int = 512,
            timeout_seconds: int = 30,
            environment: dict = None,
            **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repo_name, tag = parse_image_uri(image_uri)
        repository = ecr.Repository.from_repository_name(self, "Repo", repo_name)

        fn = _lambda.DockerImageFunction(
            self,
            "ApiFunction",
            function_name=function_name,
            code=_lambda.DockerImageCode.from_ecr(repository=repository, tag_or_digest=tag),
            timeout=Duration.seconds(timeout_seconds),
            memory_size=memory_size,
            environment=environment or {},
        )

        integration = integrations.HttpLambdaIntegration("Integration", fn)

        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"{function_name}-api",
            default_integration=integration,
        )

        CfnOutput(self, "ApiUrl", value=http_api.url or "")
        CfnOutput(self, "FunctionArn", value=fn.function_arn)
        CfnOutput(self, "FunctionName", value=fn.function_name)