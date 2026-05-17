import subprocess
result = subprocess.run(['okx', 'orders', 'list', '--instId', 'DOGE-USDT-SWAP', '--instType', 'SWAP', '--state', 'alive'], 
                       capture_output=True, text=True, timeout=15)
print(result.stdout[:3000])
if result.stderr:
    print('STDERR:', result.stderr[:1000])
