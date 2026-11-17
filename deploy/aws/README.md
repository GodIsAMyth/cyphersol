# AWS Deployment Guide

CypherSOL can be deployed on AWS using two recommended patterns:
1. **AWS ECS Fargate with Amazon EFS** (Recommended for production persistent multi-case storage).
2. **AWS App Runner** (Simplest fully-managed container service).

---

## Option 1: AWS ECS Fargate + Amazon EFS (Recommended)

ECS Fargate runs serverless containers, and Amazon EFS provides durable, multi-AZ persistent network storage mounted directly into `/data`.

### Steps:
1. **Create ECR Repository**:
   ```bash
   aws ecr create-repository --repository-name cyphersol --region <region>
   ```

2. **Build, Tag, and Push Docker Image**:
   ```bash
   ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
   REGION="ap-south-1" # Mumbai (or us-east-1)

   aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com
   docker build -t cyphersol .
   docker tag cyphersol:latest $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/cyphersol:latest
   docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/cyphersol:latest
   ```

3. **Create Amazon EFS File System**:
   ```bash
   EFS_ID=$(aws efs create-file-system \
     --performance-mode generalPurpose \
     --throughput-mode bursting \
     --encrypted \
     --region $REGION \
     --query 'FileSystemId' --output text)
   echo "Created EFS: $EFS_ID"
   ```
   Create mount targets in your VPC subnets allowing port 2049 inbound.

4. **Register ECS Task Definition**:
   Replace `ACCOUNT_ID`, `REGION`, and `EFS_ID` in `deploy/aws/ecs-task-definition.json`:
   ```bash
   sed -i '' "s/ACCOUNT_ID/$ACCOUNT_ID/g" deploy/aws/ecs-task-definition.json
   sed -i '' "s/REGION/$REGION/g" deploy/aws/ecs-task-definition.json
   sed -i '' "s/fs-EFS_ID/$EFS_ID/g" deploy/aws/ecs-task-definition.json

   aws ecs register-task-definition --cli-input-json file://deploy/aws/ecs-task-definition.json
   ```

5. **Create ECS Service**:
   Create a Fargate service with an Application Load Balancer (ALB) mapping HTTPS 443 to container port 8080.

---

## Option 2: AWS App Runner

1. Connect your GitHub repository or push image to Amazon ECR.
2. In AWS Console -> App Runner -> **Create Service**:
   - Source: Container registry (`$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/cyphersol:latest`)
   - Port: `8080`
   - CPU: `1 vCPU`, Memory: `2 GB`
   - Environment variables:
     - `HOST=0.0.0.0`
     - `PORT=8080`
     - `CYPHERSOL_DATA_DIR=/data`
     - `CYPHERSOL_ALLOWED_HOSTS=*`
     - `CYPHERSOL_NO_BROWSER=1`
