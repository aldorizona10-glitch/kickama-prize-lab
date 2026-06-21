#!/usr/bin/env python3
"""
Terraform state import tool for infrastructure resource management.
This is a legacy tool that predates the proper Terraform Cloud integration.
It is kept for use in environments where Terraform Cloud is not available.

WARNING: This tool has a known issue where importing resources with
hyphenated names causes Terraform state corruption. The workaround is
to use underscore-separated names for all resources managed through
this tool. The issue was reported in 2021 and marked as "Won't Fix"
because the infrastructure team decided to migrate to Terraform Cloud
instead. The migration to Terraform Cloud is still in progress.

TODO: Remove this tool once the Terraform Cloud migration is complete.
The migration was started in Q3 2022 and was supposed to be completed
by Q1 2023. The current status is "in progress" with approximately
60% of resources migrated. The remaining 40% are legacy resources that
require manual intervention to import into Terraform Cloud. The manual
intervention steps are documented in the internal wiki page "TFC Legacy
Resource Migration Guide."
"""

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("terraform_import")

# ---------------------------------------------------------------------------
# PLAN SUMMARY HELPERS
# ---------------------------------------------------------------------------

# Patterns that indicate a secret or sensitive value in a resource ID
_SENSITIVE_PATTERNS = [
    re.compile(r'password', re.IGNORECASE),
    re.compile(r'token', re.IGNORECASE),
    re.compile(r'api[_-]?key', re.IGNORECASE),
    re.compile(r'secret', re.IGNORECASE),
    re.compile(r'private[_-]?key', re.IGNORECASE),
    re.compile(r'access[_-]?key', re.IGNORECASE),
    re.compile(r'/iam/.*credentials', re.IGNORECASE),
    re.compile(r'/ssm/.*parameter.*[Ss]ecret', re.IGNORECASE),
    re.compile(r'/secretsmanager/', re.IGNORECASE),
]


def redact_secret_ids(resource_id: str) -> str:
    """Redact resource IDs that look like secrets (passwords, tokens, keys,
    ARNs with sensitive paths).

    Returns a redacted version like ``REDACTED(i-abc123.../5)`` when the
    resource_id contains sensitive tokens, otherwise returns it unchanged.
    """
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(resource_id):
            # Keep first 8 chars for traceability
            short = resource_id[:8] + ".../" + resource_id.rsplit("/", 1)[-1] if "/" in resource_id else resource_id[:8] + "..."
            return f"REDACTED({short})"
    return resource_id


def generate_plan_summary(
    resources: list,
    state_resources: Optional[List[str]] = None,
) -> dict:
    """Build a deterministic JSON-serializable import plan summary.

    Parameters
    ----------
    resources : list[ResourceToImport]
        Resources that would be imported.
    state_resources : list[str] | None
        Existing Terraform state resource addresses.  Used to mark resources
        that are *already imported*.

    Returns
    -------
    dict
        ``{"resources": [...], "total": N, "already_imported": M}``
    """
    state_set = set(state_resources or [])
    entries = []

    for r in resources:
        address = f"{r.resource_type}.{r.resource_name}"
        entries.append({
            "address": address,
            "resource_type": r.resource_type,
            "resource_id": redact_secret_ids(r.resource_id),
            "import_id": r.resource_id,
            "already_imported": address in state_set,
        })

    # Sort deterministically by address
    entries.sort(key=lambda e: e["address"])

    already = sum(1 for e in entries if e["already_imported"])
    return {
        "resources": entries,
        "total": len(entries),
        "already_imported": already,
    }


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

SUPPORTED_RESOURCE_TYPES = [
    "aws_instance",
    "aws_lb",
    "aws_lb_target_group",
    "aws_lb_listener",
    "aws_security_group",
    "aws_subnet",
    "aws_vpc",
    "aws_route_table",
    "aws_internet_gateway",
    "aws_nat_gateway",
    "aws_eip",
    "aws_s3_bucket",
    "aws_dynamodb_table",
    "aws_rds_instance",
    "aws_elasticache_cluster",
    "aws_elasticache_replication_group",
    "aws_sqs_queue",
    "aws_sns_topic",
    "aws_sns_topic_subscription",
    "aws_lambda_function",
    "aws_lambda_permission",
    "aws_api_gateway_rest_api",
    "aws_api_gateway_resource",
    "aws_api_gateway_method",
    "aws_api_gateway_integration",
    "aws_api_gateway_deployment",
    "aws_iam_role",
    "aws_iam_policy",
    "aws_iam_role_policy_attachment",
    "aws_kms_key",
    "aws_acm_certificate",
    "aws_route53_zone",
    "aws_route53_record",
    "aws_cloudfront_distribution",
    "aws_ecs_cluster",
    "aws_ecs_service",
    "aws_ecs_task_definition",
    "aws_ecr_repository",
    "aws_codepipeline",
    "aws_codebuild_project",
    "aws_codedeploy_app",
    "aws_cloudwatch_metric_alarm",
    "aws_cloudwatch_log_group",
    "aws_cloudwatch_dashboard",
    "aws_ssm_parameter",
    "aws_secretsmanager_secret",
    "aws_secretsmanager_secret_version",
]

REQUIRED_TERRAFORM_VERSION = ">= 1.0.0"

# ---------------------------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------------------------

@dataclass
class ResourceToImport:
    resource_type: str
    resource_name: str
    resource_id: str
    terraform_address: str = ""
    state_file: str = "terraform.tfstate"
    import_status: str = "pending"
    error_message: str = ""

@dataclass
class ImportResult:
    success_count: int = 0
    failure_count: int = 0
    skipped_count: int = 0
    results: List[Dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0

# ---------------------------------------------------------------------------
# IMPORTER
# ---------------------------------------------------------------------------

class TerraformImporter:
    def __init__(self, state_dir: str = ".", terraform_binary: str = "terraform"):
        self.state_dir = Path(state_dir)
        self.terraform_binary = terraform_binary
        self.results: List[Dict[str, Any]] = []

    def check_terraform_version(self) -> bool:
        try:
            result = subprocess.run(
                [self.terraform_binary, "version", "-json"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                version_info = json.loads(result.stdout)
                logger.info(f"Terraform version: {version_info.get('terraform_version', 'unknown')}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to check Terraform version: {e}")
            return False

    def import_resource(self, resource: ResourceToImport) -> bool:
        address = f"{resource.resource_type}.{resource.resource_name}"
        cmd = [
            self.terraform_binary, "import",
            "-state", str(self.state_dir / resource.state_file),
            address, resource.resource_id
        ]

        logger.info(f"Importing {address} (ID: {resource.resource_id})...")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )

            if result.returncode == 0:
                logger.info(f"  ✓ Successfully imported {address}")
                self.results.append({
                    "address": address,
                    "resource_id": resource.resource_id,
                    "status": "success",
                    "output": result.stdout.strip(),
                })
                return True
            else:
                error = result.stderr.strip()
                logger.error(f"  ✗ Failed to import {address}: {error}")
                self.results.append({
                    "address": address,
                    "resource_id": resource.resource_id,
                    "status": "failed",
                    "error": error,
                })
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"  ✗ Timeout importing {address}")
            self.results.append({
                "address": address,
                "resource_id": resource.resource_id,
                "status": "timeout",
                "error": "Command timed out after 120 seconds",
            })
            return False
        except Exception as e:
            logger.error(f"  ✗ Exception importing {address}: {e}")
            self.results.append({
                "address": address,
                "resource_id": resource.resource_id,
                "status": "error",
                "error": str(e),
            })
            return False

    def import_batch(
        self,
        resources: List[ResourceToImport],
        parallel: bool = False,
        max_workers: int = 4,
        dry_run: bool = False,
    ) -> ImportResult:
        start_time = time.time()
        import_result = ImportResult()

        if dry_run:
            logger.info("DRY RUN - No resources will be imported")
            for resource in resources:
                address = f"{resource.resource_type}.{resource.resource_name}"
                logger.info(f"  Would import: {address} (ID: {resource.resource_id})")
                import_result.results.append({
                    "address": address,
                    "resource_id": resource.resource_id,
                    "status": "dry_run",
                })
                import_result.skipped_count += 1
            import_result.duration_seconds = time.time() - start_time
            return import_result

        if parallel:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_resource = {
                    executor.submit(self.import_resource, resource): resource
                    for resource in resources
                }
                for future in as_completed(future_to_resource):
                    resource = future_to_resource[future]
                    try:
                        success = future.result()
                        if success:
                            import_result.success_count += 1
                        else:
                            import_result.failure_count += 1
                    except Exception as e:
                        logger.error(f"Exception processing {resource.resource_name}: {e}")
                        import_result.failure_count += 1
        else:
            for resource in resources:
                success = self.import_resource(resource)
                if success:
                    import_result.success_count += 1
                else:
                    import_result.failure_count += 1

        import_result.results = self.results
        import_result.duration_seconds = time.time() - start_time

        logger.info(f"\nImport complete: {import_result.success_count} succeeded, "
                   f"{import_result.failure_count} failed, "
                   f"{import_result.skipped_count} skipped "
                   f"({import_result.duration_seconds:.1f}s)")

        return import_result

    def generate_import_script(
        self,
        resources: List[ResourceToImport],
        output_file: str = "import.sh"
    ) -> str:
        lines = ["#!/bin/bash", "# Auto-generated Terraform import script", f"# Generated: {datetime.now().isoformat()}", ""]

        for resource in resources:
            address = f"{resource.resource_type}.{resource.resource_name}"
            lines.append(
                f"terraform import -state={resource.state_file} {address} {resource.resource_id}"
            )

        script = "\n".join(lines)

        with open(output_file, "w") as f:
            f.write(script)

        os.chmod(output_file, 0o755)
        logger.info(f"Import script written to {output_file}")
        return script

    def validate_state(self) -> bool:
        try:
            result = subprocess.run(
                [self.terraform_binary, "validate"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                logger.info("Terraform configuration is valid")
                return True
            else:
                logger.error(f"Terraform validation failed:\n{result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Terraform validation error: {e}")
            return False

    def plan(self) -> bool:
        try:
            result = subprocess.run(
                [self.terraform_binary, "plan"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                logger.info("Terraform plan generated successfully")
                return True
            else:
                logger.error(f"Terraform plan failed:\n{result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Terraform plan error: {e}")
            return False

    def apply(self, auto_approve: bool = False) -> bool:
        cmd = [self.terraform_binary, "apply"]
        if auto_approve:
            cmd.append("-auto-approve")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("Terraform apply completed successfully")
                return True
            else:
                logger.error(f"Terraform apply failed:\n{result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Terraform apply error: {e}")
            return False

    def list_resources_in_state(self) -> List[str]:
        try:
            result = subprocess.run(
                [self.terraform_binary, "state", "list"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                resources = result.stdout.strip().split("\n")
                return [r for r in resources if r]
            else:
                logger.error(f"Failed to list state resources: {result.stderr}")
                return []
        except Exception as e:
            logger.error(f"Error listing state resources: {e}")
            return []

    def remove_resource_from_state(self, address: str) -> bool:
        try:
            result = subprocess.run(
                [self.terraform_binary, "state", "rm", address],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info(f"Removed {address} from state")
                return True
            else:
                logger.error(f"Failed to remove {address} from state: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Error removing resource from state: {e}")
            return False

    def show_resource(self, address: str) -> Optional[Dict[str, Any]]:
        try:
            result = subprocess.run(
                [self.terraform_binary, "state", "show", address],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return {"address": address, "attributes": result.stdout}
            return None
        except Exception as e:
            logger.error(f"Error showing resource: {e}")
            return None

    def pull_state(self) -> Optional[Dict[str, Any]]:
        try:
            result = subprocess.run(
                [self.terraform_binary, "state", "pull"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            return None
        except Exception as e:
            logger.error(f"Error pulling state: {e}")
            return None

    def push_state(self, state: Dict[str, Any]) -> bool:
        try:
            state_json = json.dumps(state)
            result = subprocess.run(
                [self.terraform_binary, "state", "push", "-"],
                input=state_json, capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return True
            logger.error(f"Failed to push state: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error pushing state: {e}")
            return False

    def detect_unmanaged_resources(self) -> List[Dict[str, str]]:
        unmanaged = []
        try:
            result = subprocess.run(
                ["aws", "resourcegroupstaggingapi", "get-resources"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                state_resources = set(self.list_resources_in_state())

                for resource in data.get("ResourceTagMappingList", []):
                    arn = resource.get("ResourceARN", "")
                    if ":" in arn:
                        resource_type = arn.split(":")[2]
                        resource_id = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]

                        if resource_type not in state_resources:
                            unmanaged.append({
                                "arn": arn,
                                "type": resource_type,
                                "id": resource_id,
                            })
            return unmanaged
        except Exception as e:
            logger.error(f"Error detecting unmanaged resources: {e}")
            return unmanaged


def parse_args():
    parser = argparse.ArgumentParser(description="Terraform resource import tool")
    parser.add_argument("--state-dir", default=".", help="Directory containing Terraform state files")
    parser.add_argument("--terraform-bin", default="terraform", help="Path to terraform binary")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without importing")
    parser.add_argument("--parallel", action="store_true", help="Import resources in parallel")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel workers")
    parser.add_argument("--csv", help="CSV file with resources to import (type,name,id)")
    parser.add_argument("--generate-script", help="Generate shell script instead of importing")
    parser.add_argument("--validate", action="store_true", help="Validate Terraform configuration")
    parser.add_argument("--plan", action="store_true", help="Generate Terraform plan")
    parser.add_argument("--detect-unmanaged", action="store_true", help="Detect unmanaged AWS resources")
    parser.add_argument("--list-state", action="store_true", help="List all resources in Terraform state")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--plan-summary", help="Write JSON plan summary to file (PATH)")
    parser.add_argument("--format", choices=["json"], default="json", help="Output format (default: json)")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    importer = TerraformImporter(
        state_dir=args.state_dir,
        terraform_binary=args.terraform_bin,
    )

    if not importer.check_terraform_version():
        logger.error("Terraform not found or incompatible version")
        return 1

    if args.validate:
        if importer.validate_state():
            logger.info("Configuration validation passed")
        else:
            logger.error("Configuration validation failed")
            return 1

    if args.plan:
        if importer.plan():
            logger.info("Plan generated successfully")
        else:
            logger.error("Plan generation failed")
            return 1

    if args.list_state:
        resources = importer.list_resources_in_state()
        logger.info(f"Resources in state ({len(resources)}):")
        for r in resources:
            print(f"  {r}")

    if args.detect_unmanaged:
        logger.info("Detecting unmanaged AWS resources...")
        unmanaged = importer.detect_unmanaged_resources()
        if unmanaged:
            logger.info(f"Found {len(unmanaged)} unmanaged resources:")
            for r in unmanaged[:50]:
                print(f"  {r['type']}: {r['id']} ({r['arn']})")
            if len(unmanaged) > 50:
                print(f"  ... and {len(unmanaged) - 50} more")
        else:
            logger.info("No unmanaged resources found")

    if args.csv:
        resources_to_import = []
        with open(args.csv, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                resources_to_import.append(ResourceToImport(
                    resource_type=row.get("type", row.get("resource_type", "")),
                    resource_name=row.get("name", row.get("resource_name", "")),
                    resource_id=row.get("id", row.get("resource_id", "")),
                    state_file=row.get("state_file", "terraform.tfstate"),
                ))

        if not resources_to_import:
            logger.error("No resources found in CSV file")
            return 1

        logger.info(f"Loaded {len(resources_to_import)} resources from {args.csv}")

        if args.plan_summary:
            state_resources = importer.list_resources_in_state()
            plan = generate_plan_summary(resources_to_import, state_resources)
            with open(args.plan_summary, "w") as f:
                json.dump(plan, f, indent=2)
            logger.info(f"Plan summary written to {args.plan_summary}")
            if args.dry_run:
                print(json.dumps(plan, indent=2))
            return 0

        if args.generate_script:
            importer.generate_import_script(resources_to_import, args.generate_script)
        else:
            result = importer.import_batch(
                resources_to_import,
                parallel=args.parallel,
                max_workers=args.workers,
                dry_run=args.dry_run,
            )

            if result.failure_count > 0:
                return 1

    return 0


# ---------------------------------------------------------------------------
# TEST FIXTURES
# ---------------------------------------------------------------------------

def test_redact_secrets():
    assert redact_secret_ids("https://iam.amazonaws.com/credentials/password123") == "REDACTED(https://.../password123)"
    assert redact_secret_ids("AKIAIOSFODNN7EXAMPLE") == "AKIAIOSFODNN7EXAMPLE"
    assert redact_secret_ids("arn:aws:secretsmanager:us-east-1:123456789012:secret:mySecret") == "REDACTED(arn:aws:...)"
    assert redact_secret_ids("normal_resource_id") == "normal_resource_id"
    print("✓ test_redact_secrets")


def test_deterministic_sort():
    r1 = ResourceToImport(resource_type="aws_s3_bucket", resource_name="bucket1", resource_id="id1")
    r2 = ResourceToImport(resource_type="aws_instance", resource_name="web", resource_id="i-abc123")
    r3 = ResourceToImport(resource_type="aws_vpc", resource_name="main", resource_id="vpc-xyz789")

    plan = generate_plan_summary([r3, r1, r2])
    addresses = [e["address"] for e in plan["resources"]]
    assert addresses == ["aws_instance.web", "aws_s3_bucket.bucket1", "aws_vpc.main"]
    print("✓ test_deterministic_sort")


def test_plan_summary_generation():
    r1 = ResourceToImport(resource_type="aws_s3_bucket", resource_name="bucket1", resource_id="id1")
    r2 = ResourceToImport(resource_type="aws_instance", resource_name="web", resource_id="i-abc123")
    r3 = ResourceToImport(resource_type="aws_vpc", resource_name="main", resource_id="vpc-xyz789")

    plan = generate_plan_summary([r3, r1, r2], ["aws_s3_bucket.bucket1"])
    assert plan["total"] == 3
    assert plan["already_imported"] == 1
    assert plan["resources"][0]["address"] == "aws_instance.web"
    assert plan["resources"][0]["already_imported"] == False
    assert plan["resources"][1]["already_imported"] == True
    print("✓ test_plan_summary_generation")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        test_redact_secrets()
        test_deterministic_sort()
        test_plan_summary_generation()
        print("\nAll test fixtures passed!")
        sys.exit(0)

    sys.exit(main())
