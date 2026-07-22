export interface AppSummary {
  name: string;
  file: string;
  repo: string;
  kind: string;
  cluster: string | null;
  region: string | null;
  awsProfile: string | null;
  syncStatus: "Synced" | "OutOfSync" | "Syncing" | "Error" | "Orphaned" | "Unknown";
  health: "Healthy" | "Progressing" | "Degraded" | "Unknown";
  message: string;
  changes: string[];
  lastSynced: string | null;
  runningCount: number | null;
  desiredCount: number | null;
  images: string[];
  labels: Record<string, string>;
  syncPaused: boolean;
}

export interface SyncPolicy {
  autoSync: boolean | null;
  selfHeal: boolean;
  prune: boolean;
}

export interface RevisionInfo {
  revision: number;
  arn: string;
  images: string[];
  registeredAt: string | null;
  current: boolean;
}

export interface AppDetail extends AppSummary {
  syncPolicy: SyncPolicy | null;
  manifest: Record<string, unknown>;
  live: {
    clusterExists: boolean;
    taskDefinition: { arn: string; revision: number; images: string[] } | null;
    service: {
      status: string;
      desiredCount: number;
      runningCount: number;
      pendingCount: number;
      taskDefinition: string;
      rolloutState: string | null;
      deployments: number;
      events: string[];
    } | null;
  };
  lastActions: string[];
}

export interface HistoryEntry {
  id: number;
  app: string;
  commit: string | null;
  status: "Succeeded" | "Error";
  message: string;
  actions: string[];
  createdAt: string | null;
}

export interface LogsResponse {
  containers: string[];
  container?: string;
  group?: string;
  events?: { timestamp: string; message: string }[];
  error?: string;
}

export interface RepoInfo {
  id: number;
  url: string;
  branch: string;
  path: string;
  authType: "https" | "ssh" | "github_app";
  hasToken: boolean;
  commit: string | null;
  message: string | null;
  author: string | null;
  error: string | null;
  lastPoll: string | null;
}

export interface ServerStatus {
  repos: RepoInfo[];
  lastPoll: string | null;
  appCount: number;
  dryRun: boolean;
  leader: boolean;
  version: string;
}

export interface TaskInfo {
  id: string;
  lastStatus: string;
  desiredStatus: string;
  healthStatus: string | null;
  taskDefinition: string;
  cpu: string | null;
  memory: string | null;
  az: string | null;
  ip: string | null;
  startedAt: string | null;
  stoppedAt?: string | null;
  stoppedReason: string | null;
  containers?: { name: string; exitCode: number | null; reason: string | null }[];
}

export interface DiffDocument {
  error?: string;
  desired: { taskDefinition: unknown; service: unknown } | null;
  live: { taskDefinition: unknown; service: unknown } | null;
}

export interface ContainerInfo {
  name: string;
  image: string;
  essential: boolean;
  cpu: number | null;
  memory: number | null;
  memoryReservation: number | null;
  portMappings: { containerPort: number; protocol?: string; hostPort?: number }[];
  environment: { name: string; value: string }[];
  secretNames: string[];
  command: string[] | null;
  logGroup: string | null;
}

export interface Resources {
  error?: string;
  cluster: {
    name: string;
    status: string;
    runningTasks: number;
    pendingTasks: number;
    activeServices: number;
    // kind: ECSCluster only
    containerInsights?: string | null;
    capacityProviders?: string[];
    defaultCapacityProviderStrategy?: { capacityProvider: string; weight?: number; base?: number }[];
    serviceConnectNamespace?: string | null;
  } | null;
  taskDefinition: {
    family: string;
    revision: number;
    arn: string;
    status: string;
    cpu: string;
    memory: string;
    networkMode: string;
    executionRoleArn: string | null;
    taskRoleArn: string | null;
    registeredAt: string | null;
    containers: ContainerInfo[];
  } | null;
  service: {
    status: string;
    launchType: string;
    desiredCount: number;
    runningCount: number;
    pendingCount: number;
    taskDefinition: string;
    createdAt: string | null;
    subnets: string[];
    securityGroups: string[];
    assignPublicIp: string | null;
    circuitBreaker: { enable: boolean; rollback: boolean } | null;
    minimumHealthyPercent: number | null;
    maximumPercent: number | null;
    deployments: {
      status: string;
      taskDefinition: string;
      desired: number;
      running: number;
      pending: number;
      failed: number;
      rolloutState: string | null;
      updatedAt: string | null;
    }[];
    events: { createdAt: string | null; message: string }[];
  } | null;
  tasks: TaskInfo[];
  stoppedTasks?: TaskInfo[];
}

export interface LogLine {
  timestamp: string;
  message: string;
}

export interface AwsProfile {
  id: number | null;
  name: string;
  region: string;
  accountId: string;
  accessKeyId: string;
}

export interface AuditEntry {
  id: number;
  user: string;
  role: string;
  action: string;
  target: string;
  detail: string;
  sourceIp: string;
  createdAt: string | null;
}

export type Role = "viewer" | "operator" | "admin";

export interface AuthInfo {
  mode: "none" | "github";
  authenticated: boolean;
  role: Role | null;
  user: { login: string; name: string | null; avatar: string | null; role?: Role } | null;
}
