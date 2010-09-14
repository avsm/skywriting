/*
 * Copyright (c) 2010 Chris Smowton <chris.smowton@cl.cam.ac.uk>
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

package uk.co.mrry.mercator.mapreduce;

import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.lang.reflect.Constructor;

import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.io.RawComparator;
import org.apache.hadoop.io.Writable;
import org.apache.hadoop.io.WritableComparable;
import org.apache.hadoop.mapred.RawKeyValueIterator;
import org.apache.hadoop.mapreduce.Counter;
import org.apache.hadoop.mapreduce.TaskAttemptID;
import org.apache.hadoop.mapreduce.OutputCommitter;
import org.apache.hadoop.mapreduce.RecordWriter;
import org.apache.hadoop.mapreduce.Reducer;
import org.apache.hadoop.util.ReflectionUtils;

import uk.co.mrry.mercator.task.Task;

public class SWReduceEntryPoint implements Task {

  @SuppressWarnings("unchecked")
  @Override
  public void invoke(FileInputStream[] fis, FileOutputStream[] fos, String[] args) {
    // This method is used to kick off the reduce stage of a Hadoop Mapreduce job running on Skywriting
    
    /* We assume that args supplies the necessary class names in the following form:
     * 1) Mapper class name
     * 2) Reducer class name
     * 3) Partitioner class name
     * 4) Input record reader class name (???)
     * 5) Input key class
     * 6) Input value class
     */
    if(args.length < 4) 
      System.out.println("[Skywriting] Insufficient arguments passed to SWReduceEntryPoint");
    
    String mapperClassName = args[0];
    String reducerClassName = args[1];
    String partitionerClassName = args[2];
    String inputRecordReaderClassName = args[3];
    String keyClassName = args[4];
    String valueClassName = args[5];
    
    try {
      
      // get the key and value classes using reflection
      Class keyClass = Class.forName(keyClassName);
      Class valueClass = Class.forName(valueClassName);
      
      // make a reducer
      Reducer reducer = (Reducer)ReflectionUtils.newInstance(Class.forName(reducerClassName), null);
      
      // TODO logic to run the reducer
      SWReduceInputMerger merger = makeMerger(keyClass, valueClass, fis);
      
      SWLineRecordWriter outCollector = makeLineWriter(keyClass, valueClass, fos[0]);
      
      Reducer.Context reducerContext = null;
      Constructor<Reducer.Context> contextConstructor =
        Reducer.Context.class.getConstructor
        (new Class[]{Reducer.class,
            Configuration.class,
            TaskAttemptID.class,
            RawKeyValueIterator.class,
            Counter.class,
            Counter.class,
            RecordWriter.class,
            OutputCommitter.class,
            RawComparator.class,
            Class.class,
            Class.class});
      
      // Create a reducer context
      reducerContext = contextConstructor.newInstance(reducer, null, null, null, null, 
          null, outCollector, null, null, null, keyClass, valueClass);
      
      // for all keys ...
      while (merger.hasMoreKeys()) {
        Iterable iter = merger.getIterator();
        // ... call the reduce method with the values provided by the iterator
        reducer.reduce(merger.getKey(), iter, reducerContext);
      }
      
    } catch (Exception e) {
      throw new RuntimeException(e);
    }
    
  }
  
  private <INKEY extends WritableComparable, INVAL extends Writable> 
  SWReduceInputMerger makeMerger(Class<INKEY> keyClass, Class<INVAL> valClass, FileInputStream[] fis) {
    return new SWReduceInputMerger<INKEY, INVAL>(fis);
  }
  
  private <OUTKEY extends WritableComparable, OUTVAL extends Writable> 
  SWLineRecordWriter makeLineWriter(Class<OUTKEY> keyClass, Class<OUTVAL> valClass, FileOutputStream output) {
    return new SWLineRecordWriter<OUTKEY, OUTVAL>(output);
  }
  

}
