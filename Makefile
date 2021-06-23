S3_BUCKET := $(shell cat .chalice/s3bucket || bash -c 'echo user-data-swap-deploy-$$RANDOM$$RANDOM' | tee .chalice/s3bucket)

logs/on_run:
	chalice logs --name on_run --follow

logs/restart:
	chalice logs --name restart --follow

logs/on_stop:
	chalice logs --name on_stop --follow

invoke/on_run:
	cat events/run-instance.json | chalice invoke --name on_run 

invoke/restart:
	cat events/instance-stopped.json | chalice invoke --name restart 

invoke/on_stop:
	cat events/instance-stopped.json | chalice invoke --name on_stop 

deploy/infra:
	aws s3api head-bucket --bucket "${S3_BUCKET}" || aws s3 mb "s3://${S3_BUCKET}"
	chalice package --merge-template ./cloudformation.json --pkg-format cloudformation --template-format json ./.chalice/packaged 
	aws cloudformation package --template-file ./.chalice/packaged/sam.json --s3-bucket "${S3_BUCKET}" --output-template-file .chalice/packaged/packaged.json
	aws cloudformation deploy --capabilities CAPABILITY_IAM --template-file ./.chalice/packaged/packaged.json --stack-name user-data-swap-cf

deploy:
	chalice deploy

