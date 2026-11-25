import hashlib,unittest
from unittest.mock import patch
import fixed_inventory_offline as m
def oid(raw):return hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()
def record(raw,sha=None,kind=b'blob'):
 return (sha or oid(raw)).encode()+b' '+kind+b' '+str(len(raw)).encode()+b'\n'+raw+b'\n'
class BatchObjectTests(unittest.TestCase):
 def read(self,raw,expected):
  with patch.object(m,'git_read',return_value=raw) as read:
   result=m._read_blob_batch('unused',expected)
  return result,read
 def test_binary_and_duplicates_exact_order_one_process(self):
  a=b'a\0\nb';b=b''
  result,read=self.read(record(a)+record(b)+record(a),[oid(a),oid(b),oid(a)])
  self.assertEqual(result,[a,b,a]);read.assert_called_once_with('unused','cat-file','--batch',input=('\n'.join([oid(a),oid(b),oid(a)])+'\n').encode())
 def test_empty_needs_no_process(self):
  with patch.object(m,'git_read') as read:self.assertEqual(m._read_blob_batch('unused',[]),[])
  read.assert_not_called()
 def test_malformed_responses_fail_closed(self):
  raw=b'x';sha=oid(raw)
  cases=[b'',sha.encode()+b' missing\n',record(raw,kind=b'tree'),record(raw,sha='a'*40),record(b'y',sha=sha),record(raw)[:-1],record(raw)+b'extra',sha.encode()+b' blob -1\n',sha.encode()+b' blob x\n',sha.encode()+b' blob 999\nx\n']
  for response in cases:
   with self.subTest(response=response):
    with self.assertRaises(m.InventoryError):self.read(response,[sha])
 def test_invalid_oid_rejected_without_process(self):
  for bad in ['HEAD','a'*39,'a'*40+'\nHEAD',None]:
   with self.subTest(bad=bad),patch.object(m,'git_read') as read:
    with self.assertRaises(m.InventoryError):m._read_blob_batch('unused',[bad])
    read.assert_not_called()

class BoundedBatchTests(unittest.TestCase):
 def test_byte_bounded_batches_preserve_duplicates_and_large_single(self):
  raws=[b'aaa',b'bbb',b'x'*11,b'aaa'];oids=[oid(x) for x in raws]
  metadata=b''.join(oid(x).encode()+b' blob '+str(len(x)).encode()+b'\n' for x in raws)
  with patch.object(m,'BATCH_BYTES',8),patch.object(m,'git_read',side_effect=[metadata,record(raws[0])+record(raws[1]),record(raws[2]),record(raws[3])]) as read:
   self.assertEqual(m.read_blobs('unused',oids),raws)
  self.assertEqual(read.call_count,4)
  self.assertEqual(read.call_args_list[0].args,('unused','cat-file','--batch-check'))
  self.assertEqual([call.kwargs['input'] for call in read.call_args_list[1:]],[(oids[0]+'\n'+oids[1]+'\n').encode(),(oids[2]+'\n').encode(),(oids[3]+'\n').encode()])
 def test_bad_metadata_stops_before_body(self):
  sha=oid(b'x')
  cases=[b'',sha.encode()+b' missing\n',sha.encode()+b' tree 1\n',b'a'*40+b' blob 1\n',sha.encode()+b' blob -1\n',sha.encode()+b' blob 1\nextra\n']
  for response in cases:
   with self.subTest(response=response),patch.object(m,'git_read',return_value=response) as read:
    with self.assertRaises(m.InventoryError):m.read_blobs('unused',[sha])
    self.assertEqual(read.call_count,1)
 def test_metadata_body_size_drift_rejected(self):
  sha=oid(b'x')
  with patch.object(m,'git_read',side_effect=[sha.encode()+b' blob 2\n',record(b'x')]):
   with self.assertRaises(m.InventoryError):m.read_blobs('unused',[sha])
