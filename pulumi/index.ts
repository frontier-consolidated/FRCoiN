import * as aws from "@pulumi/aws";
import * as docker from "@pulumi/docker";
import * as k8s from "@pulumi/kubernetes";
import * as pulumi from "@pulumi/pulumi";

const appName = `frcoin${pulumi.getStack() === "prod" ? "" : `-${pulumi.getStack()}`}`;
const appLabels = { app: appName };
const replicas = 1;

function createRepository(name: string) {
	const repository = new aws.ecr.Repository(name);

	new aws.ecr.LifecyclePolicy(
		`${name}-lifecycle-policy`,
		{
			repository: repository.name,
			policy: {
				rules: [
					{
						rulePriority: 1,
						description: "Remove untagged images",
						selection: {
							tagStatus: "untagged",
							countType: "imageCountMoreThan",
							countNumber: 1
						},
						action: {
							type: "expire"
						}
					}
				]
			}
		},
		{
			parent: repository
		}
	);

	const registry = aws.ecr
		.getAuthorizationTokenOutput({
			registryId: repository.registryId
		})
		.apply(async (credentials) => {
			return {
				server: credentials.proxyEndpoint,
				username: credentials.userName,
				password: pulumi.secret(credentials.password)
			};
		});

	return { repository, registry };
}

const config = new pulumi.Config();
const repositoryName = config.require("repositoryName");

const repository = createRepository(`${repositoryName}/bot`);
const image = new docker.Image(`${appName}-image`, {
	build: {
		context: "../",
		dockerfile: "../Dockerfile",
		platform: "linux/amd64"
	},
	imageName: repository.repository.repositoryUrl,
	registry: repository.registry
});

const namespace = new k8s.core.v1.Namespace(appName, {
	metadata: {
		name: appName
	}
});

const deployment = new k8s.apps.v1.Deployment(
	appName,
	{
		metadata: {
			namespace: namespace.metadata.name
		},
		spec: {
			selector: { matchLabels: appLabels },
			replicas,
			strategy: {
				type: "RollingUpdate",
				rollingUpdate: {
					maxSurge: 1,
					maxUnavailable: 1
				}
			},
			template: {
				metadata: {
					labels: appLabels,
					annotations: {
						"ecr/image-digest": image.repoDigest
					}
				},
				spec: {
					containers: [
						{
							name: "bot",
							image: image.imageName,
							imagePullPolicy: "Always",
							env: [
								...["DATABASE_URL", "DISCORD_TOKEN"].map((name) => ({
									name,
									value: process.env[name]
								}))
							]
						}
					],
					imagePullSecrets: [{ name: "ecr-reg-creds" }]
				}
			}
		}
	},
	{
		dependsOn: [image],
		customTimeouts: {
			create: "5m",
			update: "5m"
		}
	}
);

export const deploymentStatus = deployment.status;
