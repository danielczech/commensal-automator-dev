import redis
import time
import os
import subprocess

from .logger import log

class ProcSeticore(object):
    """This class controls data processing using seticore [1], which performs 
    upchannelisation, beamforming and narrowband doppler drift searching 
    all-in-one.

    It is designed to be imported as a processing module by the automator.

    [1] https://github.com/lacker/seticore
    """   

    def __init__(self):
        self.redis_server = redis.StrictRedis(decode_responses=True)
        self.PROC_STATUS_KEY = 'PROCSTAT'

    def process(self, seticore, hosts, bfrdir, arrayid):
        """Processes the incoming data using seticore.

        Args:
            seticore (str): Location of current version of seticore. 
            hosts (List[str]): List of processing node host names assoctated
            with the current subarray.
            bfrdir (str): Directory containing the beamformer recipe files 
            associated with the data in the NVMe modules. 
            arrayid (str): Name of the current subarray. 

        Returns:
            None
        """
        # Determine input directory:
        # Check for set of files from each node in case one of them
        # failed to record data.
        rawfiles = set() 
        for host in hosts:
            rawfiles = self.redis_server.smembers('bluse_raw_watch:{}'.format(host))
            log.info("First rawfile(s) in set: {}".format(rawfiles))
            if(len(rawfiles) > 0):
                break
        
        if(len(rawfiles) > 0):
            # Take one of the filepaths from which to determine the input directory  
            inputdirs = os.path.dirname(rawfiles.pop())
            # Get mode and determine FFT size:
            fenchan = self.redis_server.get('{}:n_channels'.format(arrayid))
            # Change FFT size to achieve roughly 1Hz channel bandwidth
            if(fenchan == '32768'):
                fft_size = str(2**14)
            elif(fenchan == '4096'):
                fft_size = str(2**17)
            elif(fenchan == '1024'):
                fft_size = str(2**19)
            else:
                log.error('Unexpected FENCHAN: {}'.format(fenchan))
                fft_size = str(2**14)
            # SB ID:
            datadir = self.redis_server.get('{}:current_sb_id'.format(arrayid))
            # Create output directories:
            outputdir = '/scratch/data/{}/seticore_search'.format(datadir)
            h5dir = '/scratch/data/{}/seticore_beamformer'.format(datadir)
            log.info('Creating output directories...') 
            log.info('\nsearch: {}\nbeamformer: {}'.format(outputdir, h5dir))
            for host in hosts:
                cmd = ['ssh', host, 'mkdir', '-p', '-m', '1777', outputdir]
                subprocess.run(cmd)
                cmd = ['ssh', host, 'mkdir', '-p', '-m', '1777', h5dir]
                subprocess.run(cmd)
            # Build slurm command:
            seticore_args = ['--input', inputdirs, 
                             '--output', outputdir, 
                             '--snr', '6',
                             '--h5_dir', h5dir, 
                             '--num_bands', '16',
                             '--fft_size', fft_size,
                             '--telescope_id', '64',
                             '--recipe_dir', bfrdir]
            err = '/home/obs/seticore_slurm/seticore_%N.err'
            out = '/home/obs/seticore_slurm/seticore_%N.out'
            cmd = ['srun', '--open-mode=append', '-e', err, '-o', out, '-w'] + [' '.join(hosts)] + [seticore] + seticore_args
            log.info('Running seticore: {}'.format(cmd))
            result = subprocess.run(cmd).returncode
            return result
        else:
            log.info('No data to process')
            return 1