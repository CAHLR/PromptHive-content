import { GetObjectCommand, PutObjectCommand, S3Client } from '@aws-sdk/client-s3';

export function createPublicationCloud({ bucket, region = 'us-west-1', client = new S3Client({ region }) }) {
  if (!bucket) throw new Error('COURSE_DOCS_RUNTIME_BUCKET is required');
  return {
    target: { bucket, region },
    async get(key) {
      try {
        const value = await client.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
        return { body: Buffer.from(await value.Body.transformToByteArray()), etag: value.ETag };
      } catch (error) {
        if (error.name === 'NoSuchKey' || error.$metadata?.httpStatusCode === 404) return null;
        throw error;
      }
    },
    async put(key, body, expectedETag) {
      return client.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body,
        ContentType: key.endsWith('.json') ? 'application/json' : key.endsWith('.csv') ? 'text/csv' : key.endsWith('.png') ? 'image/png' : key.endsWith('.jpg') || key.endsWith('.jpeg') ? 'image/jpeg' : key.endsWith('.webp') ? 'image/webp' : key.endsWith('.gif') ? 'image/gif' : 'application/octet-stream',
        ...(expectedETag ? { IfMatch: expectedETag } : { IfNoneMatch: '*' }) }));
    },
  };
}
