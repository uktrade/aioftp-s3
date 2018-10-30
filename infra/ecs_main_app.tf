
resource "aws_ecs_service" "app" {
  name            = "ftps3-app"
  cluster         = "${aws_ecs_cluster.main.id}"
  task_definition = "${aws_ecs_task_definition.app.arn}"
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = ["${aws_subnet.main.id}"]
    security_groups = ["${aws_security_group.app_service.id}"]
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = "ftps3-app"
  container_definitions    = "${data.template_file.app_container_definitions.rendered}"
  execution_role_arn       = "${aws_iam_role.app_task_execution.arn}"
  task_role_arn            = "${aws_iam_role.app_task.arn}"
  network_mode             = "awsvpc"
  cpu                      = "${local.app_container_cpu}"
  memory                   = "${local.app_container_memory}"
  requires_compatibilities = ["FARGATE"]
}

resource "aws_iam_role" "app_task_execution" {
  name               = "ftps3-app-task-execution"
  path               = "/"
  assume_role_policy = "${data.aws_iam_policy_document.app_task_execution_assume_role.json}"
}

data "aws_iam_policy_document" "app_task_execution_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "app_task_execution" {
  role       = "${aws_iam_role.app_task_execution.name}"
  policy_arn = "${aws_iam_policy.app_task_execution.arn}"
}

resource "aws_iam_policy" "app_task_execution" {
  name        = "ftps3-app-task-execution"
  path        = "/"
  policy       = "${data.aws_iam_policy_document.app_task_execution.json}"
}

data "aws_iam_policy_document" "app_task_execution" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = [
      "${aws_cloudwatch_log_group.aws_ecs_task_definition_app.arn}",
    ]
  }
}

resource "aws_iam_role" "app_task" {
  name               = "ftps3-app-task"
  path               = "/"
  assume_role_policy = "${data.aws_iam_policy_document.app_task.json}"
}

data "aws_iam_policy_document" "app_task" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "template_file" "app_container_definitions" {
  template = "${file("${path.module}/ecs_main_app_container_definitions.json_template")}"

  vars {
    container_image  = "${var.app_container_image}"
    container_name   = "${local.app_container_name}"
    container_cpu    = "${local.app_container_cpu}"
    container_memory = "${local.app_container_memory}"
    container_ports  = "${join(",", formatlist("{\"containerPort\":%s}", aws_lb_listener.app_public_data.*.port))}"

    log_group  = "${aws_cloudwatch_log_group.aws_ecs_task_definition_app.name}"
    log_region = "${data.aws_region.aws_region.name}"

    aws_access_key_id     = "${aws_iam_access_key.app_s3.id}"
    aws_secret_access_key = "${aws_iam_access_key.app_s3.secret}"
    aws_s3_bucket_host    = "s3-${aws_s3_bucket.app.region}.amazonaws.com"
    aws_s3_bucket_name    = "${aws_s3_bucket.app.id}"
    aws_s3_bucket_region  = "${aws_s3_bucket.app.region}"

    ftp_user_login       = "${var.ftp_user_login}"
    ftp_user_password    = "${var.ftp_user_password}"
    ftp_command_port     = "${var.ftp_command_port}"
    ftp_data_ports_first = "${var.ftp_data_ports_first}"
    ftp_data_ports_count = "${var.ftp_data_ports_count}"
  }
}

resource "aws_cloudwatch_log_group" "aws_ecs_task_definition_app" {
  name              = "ftps3-app"
  retention_in_days = "3653"
}
