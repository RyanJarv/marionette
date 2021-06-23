S3_BUCKET := $(shell cat .chalice/s3bucket || bash -c 'echo user-data-swap-deploy-$$RANDOM$$RANDOM' | tee .chalice/s3bucket)

logs:
	chalice logs --name main --follow

logs/on_stop:
	chalice logs --name on_stop --follow

deploy/infra:
	aws s3api head-bucket --bucket "${S3_BUCKET}" || aws s3 mb "s3://${S3_BUCKET}"
	chalice package --merge-template ./cloudformation.json --pkg-format cloudformation --template-format json ./.chalice/packaged 
	aws cloudformation package --template-file ./.chalice/packaged/sam.json --s3-bucket "${S3_BUCKET}" --output-template-file .chalice/packaged/packaged.json
	aws cloudformation deploy --capabilities CAPABILITY_IAM --template-file ./.chalice/packaged/packaged.json --stack-name user-data-swap-cf

deploy:
	chalice deploy

invoke:# deploy
	cat events/instance-stopped.json | chalice invoke --name on_stop 
